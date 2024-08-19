import torch
import torch.nn as nn
import transformers
import os
import hydra
from omegaconf import OmegaConf, DictConfig
import wandb
import json
from typing import Optional, Set
from transformers import AutoModelForCausalLM, AutoConfig, AutoTokenizer

import torch_xla.core.xla_model as xm
import torch_xla.distributed.spmd as xs
import torch_xla.runtime as xr

import numpy as np
import torch.nn.functional as F

import torch_xla
import jax

from torch_xla.experimental.spmd_fully_sharded_data_parallel import (
    SpmdFullyShardedDataParallel as FSDPv2,
)

from torch_xla.distributed.fsdp import checkpoint_module

from torch_xla.distributed.fsdp.wrap import (
    size_based_auto_wrap_policy,
    transformer_auto_wrap_policy,
)

from transformers.trainer_pt_utils import (
    get_module_class_from_name,
)

import functools
import gc
from transformers import logging

from typing import Dict, Union, List, Tuple, Literal
import torch

from datetime import datetime
import os
import getpass
from transformers import set_seed
from utils import get_synthetic_data_device_iterator, get_data_device_iterator, get_cpu_memory, print_batch
import torch_xla.debug.metrics as met
from torch_xla.experimental.distributed_checkpoint import CheckpointManager, prime_optimizer

OmegaConf.register_new_resolver("get_local_run_dir", lambda exp_name, local_dir: get_local_run_dir(exp_name, local_dir))
logger = logging.get_logger(__name__)

import torch_xla.core.xla_model as xm
import torch_xla.debug.metrics as met

import torch_xla.distributed.spmd as xs
import torch_xla.runtime as xr

import torch_xla.debug.profiler as xp
server = xp.start_server(9012)
print(f'Profiling server started: {str(server)}')


def print_param_sharding(model):
    for name, param in model.named_parameters():
        logger.info(f"{name}: {param.shape} {param.dtype} {torch_xla._XLAC._get_xla_sharding_spec(param)}")

def get_local_dir(prefix: str) -> str:
    """Return the path to the cache directory for this user."""
    if os.path.exists(prefix):
        return f"{prefix}/{getpass.getuser()}"
    os.makedirs(prefix)
    return f"{prefix}/{getpass.getuser()}"
    
def dpo_loss(
        policy_chosen_logps: torch.FloatTensor,
        policy_rejected_logps: torch.FloatTensor,
        reference_chosen_logps: torch.FloatTensor,
        reference_rejected_logps: torch.FloatTensor,
        beta: float = 0.1,
        label_smoothing: float = 0.0,
        loss_type: str = "sigmoid",
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
        """Compute the DPO loss for a batch of policy and reference model log probabilities.

        Args:
            policy_chosen_logps: Log probabilities of the policy model for the chosen responses. Shape: (batch_size,)
            policy_rejected_logps: Log probabilities of the policy model for the rejected responses. Shape: (batch_size,)
            reference_chosen_logps: Log probabilities of the reference model for the chosen responses. Shape: (batch_size,)
            reference_rejected_logps: Log probabilities of the reference model for the rejected responses. Shape: (batch_size,)

        Returns:
            A tuple of three tensors: (losses, chosen_rewards, rejected_rewards).
            The losses tensor contains the DPO loss for each example in the batch.
            The chosen_rewards and rejected_rewards tensors contain the rewards for the chosen and rejected responses, respectively.
        """
        pi_logratios = policy_chosen_logps - policy_rejected_logps
        ref_logratios = reference_chosen_logps - reference_rejected_logps

        logits = pi_logratios - ref_logratios

        # The beta is a temperature parameter for the DPO loss, typically something in the range of 0.1 to 0.5.
        # We ignore the reference model as beta -> 0. The label_smoothing parameter encodes our uncertainty about the labels and
        # calculates a conservative DPO loss.
        if loss_type == "sigmoid":
            losses = (
                -F.logsigmoid(beta * logits) * (1 - label_smoothing)
                - F.logsigmoid(-beta * logits) * label_smoothing
            )
        elif loss_type == "ipo":
            # eqn (17) of the paper where beta is the regularization parameter for the IPO loss, denoted by tau in the paper.
            losses = (logits - 1 / (2 * beta)) ** 2

        chosen_rewards = (
            beta
            * (
                policy_chosen_logps - reference_chosen_logps
            ).detach()
        )
        rejected_rewards = (
            beta
            * (
                policy_rejected_logps
                - reference_rejected_logps
            ).detach()
        )

        return losses, chosen_rewards, rejected_rewards

def get_local_run_dir(exp_name: str, local_dir: str) -> str:
    """Create a local directory to store outputs for this run, and return its path."""
    now = datetime.now()
    timestamp = now.strftime("%Y-%m-%d_%H-%M-%S_%f")
    run_dir = f"{get_local_dir(local_dir)}/{exp_name}_{timestamp}"
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def sequence_mask(lengths, maxlen=None, dtype=torch.bool):
    if maxlen is None:
        maxlen = lengths.max()
    row_vector = torch.arange(0, maxlen, 1)
    matrix = torch.unsqueeze(lengths, dim=-1)
    mask = row_vector < matrix

    mask.type(dtype)
    return mask

def report_metrics(step, loss, tracker, metrics):
    logger.info(f'{step=}, {loss=}, {tracker.rate()=}, {metrics=}')

def report_eval_metrics(step, loss, metrics):
    logger.info(f'{step=}, {loss=}, {metrics=}')

def get_batch_logps(
        logits: torch.FloatTensor,
        labels: torch.LongTensor,
        label_pad_token_id: int = -100,
    ) -> Tuple[torch.FloatTensor, torch.LongTensor]:
        """Compute the log probabilities of the given labels under the given logits.

        Args:
            logits: Logits of the model (unnormalized). Shape: (batch_size, sequence_length, vocab_size)
            labels: Labels for which to compute the log probabilities. Label tokens with a value of label_pad_token_id are ignored. Shape: (batch_size, sequence_length)
            label_pad_token_id: The label pad token id.
            is_encoder_decoder: Whether the model is an encoder-decoder model.

        Returns:
            A Tuple of two tensor of shape ((batch_size,), (batch_size,)) containing the sum of log probabilities of the given labels under the given logits in the first tensor and the number of non-masked tokens in the second tensor.
        """
        if logits.shape[:-1] != labels.shape:
            raise ValueError("Logits (batch and sequence length dim) and labels must have the same shape.")

        labels = labels[:, 1:].clone()
        logits = logits[:, :-1, :]
        loss_mask = labels != label_pad_token_id

        # dummy token; we'll ignore the losses on these tokens later
        labels = labels.masked_fill(labels == label_pad_token_id, 0)

        per_token_logps = torch.gather(logits.log_softmax(-1), dim=2, index=labels.unsqueeze(2)).squeeze(2)

        return (per_token_logps * loss_mask).sum(-1), loss_mask.sum(-1)


def cross_entropy_loss(logits, labels, pad_token_id=0):
    # Flatten the tokens
    logits = logits[..., :-1, :].contiguous()
    labels = labels[..., 1:].contiguous()
    loss_fct = nn.CrossEntropyLoss(ignore_index=pad_token_id)
    logits = logits.view(-1, logits.shape[-1])
    labels = labels.view(-1)
    # Enable model parallelism
    labels = labels.to(logits.device)
    loss = loss_fct(logits, labels)
    return loss


def forward(
        model: nn.Module, batch: Dict[str, Union[List, torch.LongTensor]], label_pad_token_id: int = -100, pad_token_id: int = 0,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
    chosen_logits = model(
        batch["chosen_input_ids"],
        attention_mask=batch["chosen_attention_mask"],
        use_cache=False,
    ).logits.to(torch.float32)
    rejected_logits = model(
        batch["rejected_input_ids"],
        attention_mask=batch["rejected_attention_mask"],
        use_cache=False,
    ).logits.to(torch.float32)
    chosen_labels = batch["chosen_labels"].clone()
    rejected_labels = batch["rejected_labels"].clone()

    chosen_logps, size_completion = get_batch_logps(chosen_logits, chosen_labels, label_pad_token_id=label_pad_token_id)
    rejected_logps, size_completion = get_batch_logps(rejected_logits, rejected_labels, label_pad_token_id=label_pad_token_id)
    nll_loss = cross_entropy_loss(chosen_logits, chosen_labels, pad_token_id)
    return (chosen_logps, rejected_logps, chosen_logits, rejected_logits, nll_loss)

def create_concatenated_batch(batch: Dict[str, Union[List, torch.LongTensor]]):
    # all items in batch are the same in length
    concatenated_batch = {}
    concatenated_batch["concatenated_input_ids"] = torch.cat((batch["chosen_input_ids"], batch["rejected_input_ids"]), dim=0)
    concatenated_batch["concatenated_attention_mask"] = torch.cat((batch["chosen_attention_mask"], batch["rejected_attention_mask"]), dim=0)
    concatenated_batch["concatenated_labels"] = torch.cat((batch["chosen_labels"], batch["rejected_labels"]), dim=0)
    return concatenated_batch

def concatenated_forward(
        model: nn.Module, batch: Dict[str, Union[List, torch.LongTensor]], label_pad_token_id: int = -100, pad_token_id: int = 0,
    ) -> Tuple[torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor, torch.FloatTensor]:
    """Run the given model on the given batch of inputs, concatenating the chosen and rejected inputs together.

    We do this to avoid doing two forward passes, because it's faster for FSDP.
    """
    concatenated_batch = create_concatenated_batch(batch)
    len_chosen = concatenated_batch["concatenated_input_ids"].shape[0] // 2
    all_logits = model(
        concatenated_batch["concatenated_input_ids"],
        attention_mask=concatenated_batch["concatenated_attention_mask"],
        use_cache=False,
    ).logits.to(torch.float32)

    all_logps, size_completion = get_batch_logps(
        all_logits,
        concatenated_batch["concatenated_labels"],
        label_pad_token_id=label_pad_token_id,
    )

    labels = concatenated_batch["concatenated_labels"].clone()
    nll_loss = cross_entropy_loss(all_logits[:len_chosen], batch["chosen_input_ids"], pad_token_id)

    chosen_logps = all_logps[:len_chosen]
    rejected_logps = all_logps[len_chosen:]

    chosen_logits = all_logits[:len_chosen]
    rejected_logits = all_logits[len_chosen:]

    return (chosen_logps, rejected_logps, chosen_logits, rejected_logits, nll_loss)


def get_batch_loss_metrics(
        model,
        ref_model,
        batch: Dict[str, Union[List, torch.LongTensor]],
        train_eval: Literal["train", "eval"] = "train",
        label_pad_token_id: int = -100,
        pad_token_id: int = 0,
        beta: float = 0.1,
        config: DictConfig = None,
    ):
    """Compute the DPO loss and other metrics for the given batch of inputs for train or test."""
    metrics = {}

    if config.concatenated_forward:
        (
            policy_chosen_logps,
            policy_rejected_logps,
            policy_chosen_logits,
            policy_rejected_logits,
            policy_chosen_logps_avg,
        ) = concatenated_forward(model, batch, label_pad_token_id, pad_token_id)
    else:
        (
            policy_chosen_logps,
            policy_rejected_logps,
            policy_chosen_logits,
            policy_rejected_logits,
            policy_chosen_logps_avg,
        ) = forward(model, batch, label_pad_token_id, pad_token_id)

    with torch.no_grad():
        if config.concatenated_forward:
            (
                reference_chosen_logps,
                reference_rejected_logps,
                _,
                _,
                _,
            ) = concatenated_forward(ref_model, batch, label_pad_token_id, pad_token_id)
        else:
            (
                reference_chosen_logps,
                reference_rejected_logps,
                _,
                _,
                _,
            ) = forward(ref_model, batch, label_pad_token_id, pad_token_id)

    losses, chosen_rewards, rejected_rewards = dpo_loss(
        policy_chosen_logps,
        policy_rejected_logps,
        reference_chosen_logps,
        reference_rejected_logps,
        beta,
    )
    reward_accuracies = (chosen_rewards > rejected_rewards).float()

    prefix = "eval_" if train_eval == "eval" else ""
    num_samples = batch["chosen_input_ids"].shape[0]
    # TODO
    # mute metrics now since it trigger recompile in pytorch xla
    metrics[f"{prefix}rewards/chosen"] = chosen_rewards.sum()
    metrics[f"{prefix}rewards/rejected"] = rejected_rewards.sum()
    metrics[f"{prefix}rewards/accuracies"] = reward_accuracies.sum()
    metrics[f"{prefix}rewards/margins"] = (chosen_rewards - rejected_rewards).sum()
    metrics[f"{prefix}logps/rejected"] = policy_rejected_logps.detach().sum()
    metrics[f"{prefix}logps/chosen"] = policy_chosen_logps.detach().sum()
    metrics[f"{prefix}logits/rejected"] = policy_rejected_logits.detach().sum()
    metrics[f"{prefix}logits/chosen"] = policy_chosen_logits.detach().sum()
    metrics[f"{prefix}losses"] = losses.detach().sum()
    metrics[f"{prefix}num_samples"] = num_samples
    metrics[f"{prefix}ppl"] = torch.exp(policy_chosen_logps_avg.detach())

    return losses.mean(), metrics


def prepare_model(model, config):
    def shard_output(output, mesh):
        from transformers.modeling_outputs import CausalLMOutputWithPast

        real_output = None
        if isinstance(output, torch.Tensor):
            real_output = output
        elif isinstance(output, tuple):
            real_output = output[0]
        elif hasattr(output, "logits"):
            real_output = output.logits

        if real_output is None:
            raise ValueError("Something went wrong, the output of the model shouldn't be `None`")
        xs.mark_sharding(real_output, mesh, ("fsdp", None, None))

    auto_wrap_policy = None
    auto_wrapper_callable = None

    default_transformer_cls_names_to_wrap = getattr(model, "_no_split_modules", None)
    fsdp_transformer_layer_cls_to_wrap = config.model.fsdp_config.get(
        "transformer_layer_cls_to_wrap", default_transformer_cls_names_to_wrap
    )

    if config.model.fsdp_config["min_num_params"] > 0:
        auto_wrap_policy = functools.partial(
            size_based_auto_wrap_policy, min_num_params=config.model.fsdp_config["min_num_params"]
        )
    elif fsdp_transformer_layer_cls_to_wrap is not None:
        transformer_cls_to_wrap = set()
        for layer_class in fsdp_transformer_layer_cls_to_wrap:
            transformer_cls = get_module_class_from_name(model, layer_class)
            if transformer_cls is None:
                raise Exception("Could not find the transformer layer class to wrap in the model.")
            else:
                transformer_cls_to_wrap.add(transformer_cls)

        auto_wrap_policy = functools.partial(
            transformer_auto_wrap_policy,
            # Transformer layer class to wrap
            transformer_layer_cls=transformer_cls_to_wrap,
        )

    if config.model.fsdp_config["xla_fsdp_grad_ckpt"]:
        if model.config.use_cache:
            logger.warning_once(
                "`use_cache=True` is incompatible with gradient checkpointing. Setting `use_cache=False`."
            )
            model.config.use_cache = False
        # Apply gradient checkpointing to auto-wrapped sub-modules if specified
        def auto_wrapper_callable(m, *args, **kwargs):
            target_cls = FSDPv2
            return target_cls(checkpoint_module(m), *args, **kwargs)

    model = FSDPv2(
                model,
                shard_output=shard_output,
                auto_wrap_policy=auto_wrap_policy,
                auto_wrapper_callable=auto_wrapper_callable,
            )
    
    return model


def clip_gradient(model, config):
    """Clip the gradient norm of the parameters of an FSDP policy, gathering the gradients across all GPUs."""
    return torch.nn.utils.clip_grad_norm_(model.parameters(), config.max_grad_norm)

def eval_fn_ppl(model, eval_device_loader):
    total_losses = []
    for eval_batch in eval_device_loader:
        model.eval()
        with torch.no_grad():
            loss = model(
                eval_batch["chosen_input_ids"],
                attention_mask=eval_batch["chosen_attention_mask"],
                labels=eval_batch["chosen_input_ids"],
                use_cache=False,
                ).loss
            total_losses.append(loss.float())

    average_loss = sum(total_losses) / len(total_losses)
    average_ppl = torch.exp(average_loss)
    logger.info(f"{average_ppl=}")


def eval_fn(model, ref_model, eval_device_loader, config, step):
    prefix = 'eval_'
    group_eval_metrics = {
        f"{prefix}rewards/chosen": [],
        f"{prefix}rewards/rejected": [],
        f"{prefix}rewards/accuracies": [],
        f"{prefix}rewards/margins": [],
        f"{prefix}logps/rejected": [],
        f"{prefix}logps/chosen": [],
        f"{prefix}logits/rejected": [],
        f"{prefix}logits/chosen": [],
        f"{prefix}losses": [],
        f"{prefix}num_samples": [],
        f"{prefix}ppl": [],
    }

    for eval_batch in eval_device_loader:
        model.eval()
        with torch.no_grad():
            _, eval_metrics = get_batch_loss_metrics(model, ref_model, eval_batch, "eval", beta=config.beta, config=config)
        for k in group_eval_metrics:
            group_eval_metrics[k].append(eval_metrics[k])

    for k, v in group_eval_metrics.items():
        # ppl is per token metrics which was averged
        if k == f"{prefix}ppl":
            group_eval_metrics[k] = sum(v) / len(v)
        else:
            group_eval_metrics[k] = sum(v)

    for k, v in group_eval_metrics.items():
        if k not in (f"{prefix}num_samples", f"{prefix}ppl"):
            group_eval_metrics[k] /= group_eval_metrics[f'{prefix}num_samples']

    num_devices = xr.global_runtime_device_count()
    global_batch_size = int(config.per_device_train_batch_size * num_devices)
    group_eval_metrics['trained_examples'] = step * global_batch_size

    xm.add_step_closure(
        report_eval_metrics, args=(step, group_eval_metrics[f"{prefix}losses"], group_eval_metrics))


def train_step(model, ref_model, train_device_loader, config, step, tracker, optimizer, global_batch_size, scheduler, start_step, tokenizer):
    batch = next(train_device_loader)
    if step == start_step:
        print_batch(batch, tokenizer)
    optimizer.zero_grad()
    model.train()
    loss, metrics = get_batch_loss_metrics(model, ref_model, batch, "train", beta=config.beta, config=config)
    tracker.add(global_batch_size)

    loss.backward()
    if config.max_grad_norm > 0.:
        grad_norm = clip_gradient(model, config)
        metrics['grad_norm'] = grad_norm
    xm.optimizer_step(optimizer)
    scheduler.step()
    return loss, metrics


@hydra.main(version_base=None, config_path="config", config_name="config")
def main(config: DictConfig):
    OmegaConf.resolve(config)
    set_seed(config.seed)
    if config.full_precision:
        assert config.model.policy_dtype == "float32" and config.model.reference_dtype == "float32", "both dtype of policy and reference need to be float32"
        torch_xla._XLAC._xla_set_use_full_mat_mul_precision(use_full_mat_mul_precision=True)
        jax.config.update("jax_default_matmul_precision", "highest")

    logger.info("\n\n************** Experiment configuration ***********")
    logger.info(OmegaConf.to_yaml(config))

    config_path = os.path.join(config.local_run_dir, 'config.yaml')
    with open(config_path, 'w') as f:
        OmegaConf.save(config, f)

    num_devices = xr.global_runtime_device_count()
    mesh_shape = (num_devices, 1)
    device_ids = np.array(range(num_devices))
    mesh = xs.Mesh(device_ids, mesh_shape, axis_names=("fsdp", "tensor") )
    xs.set_global_mesh(mesh)

    if config.model.name_or_path == "mistralai/Mixtral-8x22B-v0.1":
        # sentencepiece mismatch in a recent commit https://huggingface.co/mistralai/Mixtral-8x22B-v0.1/discussions/9
        # https://huggingface.co/mistralai/Mixtral-8x22B-v0.1/discussions/10
        # tokenizer = AutoTokenizer.from_pretrained(config.model.name_or_path, revision="refs/pr/10")
        tokenizer = AutoTokenizer.from_pretrained(config.model.name_or_path, revision="refs/pr/10")
    else:
        tokenizer = AutoTokenizer.from_pretrained(config.model.name_or_path)
    if tokenizer.chat_template is None:
        tokenizer.chat_template = "{% for message in messages %}{{message['role'] + ': ' + message['content'] + '\n\n'}}{% endfor %}{{ eos_token }}"

    policy_dtype = getattr(torch, config.model.policy_dtype)

    logger.info(f"cpu memory usage: {get_cpu_memory()}")
    logger.info("loading model")
    if config.model.config_path:
        model_config = AutoConfig.from_pretrained(config.model.config_path)
        model_config.static = True
        model_config.flash_attention = config.flash_attention
        model_config.gmm = False
        model_config.gmm_stack = False
        with torch.device("meta"):
            model = AutoModelForCausalLM.from_config(model_config).to_empty(device=xm.xla_device()).to(torch.bfloat16)
    else:
        model = AutoModelForCausalLM.from_pretrained(
            config.model.name_or_path, cache_dir=config.cache_local_dir, low_cpu_mem_usage=True, torch_dtype=policy_dtype)

    if model.config.architectures == ["MixtralForCausalLM"]:
        for layer in model.model.layers:
            layer.self_attn.rotary_emb._set_buffer(device=xm.xla_device())
    
    
    if tokenizer.vocab_size != model.config.vocab_size:
        logger.warning(
            f"Found mismatch between {tokenizer.vocab_size=} and {model.config.vocab_size}"
        )

    logger.info("model loaded")
    model = prepare_model(model, config)
    model = model.to(policy_dtype)
    logger.info("model prepared")

    gc.collect()
    xm.mark_step()
    logger.info(f"cpu memory usage: {get_cpu_memory()}")

    reference_dtype = getattr(torch, config.model.reference_dtype)
    logger.info("loading ref_model")
    if config.model.config_path:
        with torch.device("meta"):
            ref_model = AutoModelForCausalLM.from_config(model_config).to_empty(device=xm.xla_device()).to(torch.bfloat16)
    else:
        ref_model = AutoModelForCausalLM.from_pretrained(
            config.model.name_or_path, cache_dir=config.cache_local_dir, low_cpu_mem_usage=True, torch_dtype=reference_dtype)

    if ref_model.config.architectures == ["MixtralForCausalLM"]:
        for layer in ref_model.model.layers:
            layer.self_attn.rotary_emb._set_buffer(device=xm.xla_device())
    logger.info("ref_model loaded")
    ref_model.eval()
    ref_model = prepare_model(ref_model, config)
    ref_model = ref_model.to(reference_dtype)

    logger.info("ref_model prepared")
    gc.collect()
    xm.mark_step()
    logger.info(f"cpu memory usage: {get_cpu_memory()}")
    if config.use_synthetic_data:
        train_device_loader, eval_device_loader = get_synthetic_data_device_iterator(config, tokenizer, mesh)
    else:
        train_device_loader, eval_device_loader = get_data_device_iterator(config, tokenizer, mesh)

    global_batch_size = config.per_device_train_batch_size * num_devices
    # 'chosen_input_ids', 'chosen_attention_mask', 'rejected_input_ids', 'rejected_attention_mask', 'chosen_labels', 'rejected_labels'

    if config.checkpoint_manager_path:
        torch.distributed.init_process_group('gloo', init_method='xla://')
        logger.info(f"checkpoint found from {config.checkpoint_manager_path=}")

        ckpt_manager = CheckpointManager(
            path=config.checkpoint_manager_path,
            save_interval=float('inf'),
            max_to_keep=0,
        )

        state_dict = {
            'model': model.state_dict(),
        }
        ckpt_manager.restore(0, state_dict)
        for k, v in state_dict['model'].items():
            logger.info(f"{k}: {v.dtype} {v.mean()}")
        model.load_state_dict(state_dict['model'])
        ref_model.load_state_dict(state_dict['model'])
        del state_dict
        xm.mark_step()
        logger.info("checkpoint loaded")
    else:
        if config.model.config_path:
            model.apply(model._init_weights)
            ref_model.apply(ref_model._init_weights)

    if config.optimizer == "ADAMW_TORCH_XLA":
        from torch_xla.amp.syncfree import AdamW
        optimizer = AdamW(model.parameters(), lr=config.lr)
    else:
        optimizer = getattr(torch.optim, config.optimizer)(model.parameters(), lr=config.lr)

    # initialize optimizer states
    optimizer = prime_optimizer(optimizer)
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer, lr_lambda=lambda step: min(1.0, (step + 1) / (config.warmup_steps + 1)))

    print_param_sharding(model)
    print_param_sharding(ref_model)
    start_step = 0
    tracker = xm.RateTracker()

    logger.info(f"cpu memory usage: {get_cpu_memory()}")
    step = start_step
    for step in np.arange(start_step, config.max_steps):
        if config.do_first_eval and step == start_step:
            eval_fn_ppl(model, eval_device_loader)
        if step == start_step and config.do_first_eval or step > start_step and step % config.eval_frequency == 0:
            eval_fn(model, ref_model, eval_device_loader, config, step)
        try:
            loss, metrics = train_step(model, ref_model, train_device_loader, config, step, tracker, optimizer, global_batch_size, scheduler, start_step, tokenizer)
        except StopIteration:
            break
        if step >= start_step and step % config.report_metrics_freq == 0:
            xm.add_step_closure(
                report_metrics, args=(step, loss, tracker, metrics))
        if step == config.get("profile_step", None):
            xm.wait_device_ops()
            import tempfile
            xp.trace_detached('127.0.0.1:9012', config.get("profile_logdir", tempfile.mkdtemp()), config.get("profile_duration", 20000))

    eval_fn(model, ref_model, eval_device_loader, config, step)
    if config.xla_metric_report:
        logger.info(met.metrics_report())


if __name__ == '__main__':
    main()