# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import _io
import lightning.pytorch as pl
import torch
import torch.nn.functional as F
from transformers import AutoModelForCausalLM

from nemo.collections.common.tokenizers.huggingface.auto_tokenizer import AutoTokenizer
from nemo.collections.llm import fn
from nemo.lightning import io
from nemo.utils import logging


def masked_cross_entropy(logits, targets, mask=None):
    """
    Compute the masked cross-entropy loss between logits and targets.

    If a mask is provided, the loss is computed per element, multiplied by the mask,
    and then averaged. If no mask is provided, the standard cross-entropy loss is used.

    Args:
        logits (torch.Tensor): The predicted logits with shape (N, C) where C is the number of classes.
        targets (torch.Tensor): The ground truth class indices with shape (N,).
        mask (torch.Tensor, optional): A tensor that masks the loss computation. Items marked with
            1 will be used to calculate loss, otherwise ignored. Must be broadcastable to the shape
            of the loss. Defaults to None.

    Returns:
        torch.Tensor: The computed loss as a scalar tensor.
    """
    if mask is not None:
        with torch.no_grad():
            targets.masked_fill_(mask.view(-1) == 0, -100)
            del mask
    return F.cross_entropy(logits, targets)


class HFAutoModelForCausalLM(pl.LightningModule, io.IOMixin, fn.FNMixin):
    """
    A LightningModule wrapper for AutoModelForCausalLm.

    This module wraps a LightningModule around a AutoModelForCausalLM.
    It provides functionalities for training, validation, and checkpoint saving.
    """

    def __init__(
        self,
        model_name='gpt2',
        load_pretrained_weights=True,
        tokenizer=None,
        loss_fn=masked_cross_entropy,
        model_transform=None,
        model_accelerator=None,
        trust_remote_code=False,
        default_dtype=torch.bfloat16,
        load_in_4bit=False,
        attn_implementation="sdpa",
    ):
        """
        Initialize the HFAutoModelForCausalLM.

        Args:
            model_name (str, optional): The model name or path. Defaults to 'gpt2'.
            load_pretrained_weights (bool, optional): Whether to load pretrained weights. Defaults to True.
            tokenizer (AutoTokenizer, optional): A pre-configured tokenizer. Defaults to None.
            loss_fn (callable, optional): Loss function to use. Defaults to masked_cross_entropy.
            model_transform (callable, optional): Function to transform the model after creation. Defaults to None.
            model_accelerator (callable, optional): Function to accelerate or optimize the model. Defaults to None.
            trust_remote_code (bool, optional): Whether to trust remote code during model/tokenizer loading.
                Defaults to False.
            default_dtype (torch.dtype, optional): Default data type for the model. Defaults to torch.bfloat16.
            load_in_4bit (bool, optional): Whether to load the model in 4-bit precision. Defaults to False.
            attn_implementation (str, optional): Attention implementation to use. Defaults to "sdpa".

        """
        super().__init__()
        self.save_hyperparameters()
        self.model_name = model_name
        self._tokenizer = tokenizer
        self.model = None
        self.loss_fn = loss_fn
        self.load_pretrained_weights = load_pretrained_weights
        self.is_hf_model = True
        self.model_transform = model_transform
        self.model_accelerator = model_accelerator
        self.trust_remote_code = trust_remote_code
        self.default_dtype = default_dtype
        self.load_in_4bit = load_in_4bit
        self.attn_implementation = attn_implementation

    @property
    def tokenizer(self):
        """
        Get the tokenizer for the model.

        If the tokenizer is not already initialized, it will be created using the
        `configure_tokenizer` static method.

        Returns:
            AutoTokenizer: The tokenizer associated with the model.
        """
        if self._tokenizer is None:
            self._tokenizer = HFAutoModelForCausalLM.configure_tokenizer(self.model_name, self.trust_remote_code)
        return self._tokenizer

    @tokenizer.setter
    def tokenizer(self, value):
        """
        Set the tokenizer for the model.

        Args:
            value (AutoTokenizer): The tokenizer to be used.
        """
        assert self._tokenizer is None
        self._tokenizer = value

    @staticmethod
    def configure_tokenizer(model_name, use_fast=True, trust_remote_code=False):
        """
        Configure and return a Hugging Face AutoTokenizer for the given model.

        Args:
            model_name (str): The name or path of the model.
            use_fast (bool, optional): Whether to use the fast tokenizer implementation. Defaults to True.
            trust_remote_code (bool, optional): Whether to trust remote code when loading the tokenizer.
                Defaults to False.

        Returns:
            AutoTokenizer: The instantiated tokenizer.
        """
        try:
            return AutoTokenizer(model_name, use_fast=use_fast, trust_remote_code=trust_remote_code)
        except:
            return AutoTokenizer(model_name, use_fast=not use_fast, trust_remote_code=trust_remote_code)

    def _configure_model(self, attn_implementation):
        """helper method; see also configure_model."""
        # create all your layers here
        if self.load_pretrained_weights:
            return AutoModelForCausalLM.from_pretrained(
                self.model_name,
                torch_dtype=torch.bfloat16,
                device_map="cpu",
                trust_remote_code=self.trust_remote_code,
                load_in_4bit=self.load_in_4bit,
                attn_implementation=attn_implementation,
            )
        else:
            from transformers import AutoConfig

            config = AutoConfig.from_pretrained(self.model_name, trust_remote_code=self.trust_remote_code)
            dtype = getattr(config, 'torch_dtype', self.default_dtype)
            return AutoModelForCausalLM.from_config(
                config,
                torch_dtype=dtype,
                trust_remote_code=self.trust_remote_code,
                attn_implementation=attn_implementation,
            )

    def configure_model(self):
        """
        Configure and initialize the Hugging Face model.

        Depending on the `load_pretrained_weights` flag, this method either loads
        a pretrained model or creates a model from configuration. It also applies FSDP2 and
        tensor parallelization if a device mesh is provided, and applies any additional
        accelerator function if specified.

        Raises:
            Exception: If model configuration fails.
        """
        try:
            self.model = self._configure_model(attn_implementation=self.attn_implementation)
        except ValueError as e:
            # 'does not support an attention implementation through torch.nn.functional.scaled_dot_product_attention'
            if 'does not support an attention' in str(e):
                self.model = self._configure_model(attn_implementation="eager")

        if self.model_accelerator is not None:
            from nemo.lightning.pytorch.accelerate.transformer_engine import te_accelerate

            te_accelerate(self.model, self.model_accelerator.fp8_autocast)

        self.model.train()

    def forward(self, batch):
        """
        Perform a forward pass of the model.

        Args:
            batch (dict): A dictionary of inputs that the model expects.

        Returns:
            ModelOutput: The output of the underlying Hugging Face model.
        """
        return self.model(**batch)

    def training_step(self, batch, batch_idx=None):
        """
        Execute a single training step.

        This method prepares the input batch by ensuring the required keys are present,
        performs a forward pass, reshapes the logits and labels appropriately, computes the loss
        using the defined loss function, and logs the training loss.

        Args:
            batch (dict): A dictionary containing the batch data, including 'labels' and optionally 'loss_mask'.
            batch_idx (int, optional): The index of the batch. Defaults to None.

        Returns:
            torch.Tensor: The computed loss for the batch.
        """
        labels = batch.pop('labels').to(self.model.device)
        loss_mask = batch.pop('loss_mask', None)

        # GPTSFTDataset emits `tokens` instead of `input_ids`
        if not 'input_ids' in batch and 'tokens' in batch:
            batch['input_ids'] = batch['tokens']
        batch = self._remove_extra_batch_keys(batch)

        outputs = self.forward(batch)

        # Prepare for loss calculation
        logits = outputs.logits.float()
        n_cls = logits.shape[-1]
        logits = logits.view(-1, n_cls)
        labels = labels.view(-1)

        assert logits.shape[-2] == labels.shape[-1], "Expected logits & labels to have the same length"
        loss = self.loss_fn(logits, labels, loss_mask)
        self.log('reduced_train_loss', loss, prog_bar=True, rank_zero_only=True, batch_size=1, sync_dist=False)
        return loss

    @torch.no_grad
    def validation_step(self, batch, batch_idx):
        """
        Execute a single validation step.

        This method is similar to `training_step` but without gradient computations.
        It processes the input batch, performs a forward pass, computes the loss,
        and logs the validation loss.

        Args:
            batch (dict): A dictionary containing the batch data, including 'labels' and optionally 'loss_mask'.
            batch_idx (int): The index of the batch.
        """
        labels = batch.pop('labels').to(self.model.device)
        loss_mask = batch.pop('loss_mask', None)

        # GPTSFTDataset emits `tokens` instead of `input_ids`
        if not 'input_ids' in batch and 'tokens' in batch:
            batch['input_ids'] = batch['tokens']
        batch = self._remove_extra_batch_keys(batch)

        outputs = self.forward(**batch)

        logits = outputs.logits.float()
        n_cls = logits.shape[-1]
        logits = logits.view(-1, n_cls)
        labels = labels.view(-1)

        assert logits.shape[-2] == labels.shape[-1], "Expected logits & labels to have the same length"
        loss = self.loss_fn(logits, labels, loss_mask)
        self.log('val_loss', loss, on_step=True, on_epoch=True, prog_bar=True)

    def save_pretrained(self, path, state_dict):
        """
        Save the pretrained model and tokenizer to a specified path.

        This method ensures that the model state is gathered (especially in distributed settings)
        and then saves the state dict and tokenizer. Only rank 0 or the appropriate FSDP module saves
        the files to avoid race conditions.

        Args:
            path (str): The directory path where the model and tokenizer should be saved.

        Raises:
            AssertionError: If the model has not been created prior to saving.
        """
        assert self.model is not None, "Model has to be created first."

        def pop_fqn_prefix(fqn, expected_prefix='model'):
            """pops prefix from FQN"""
            parts = fqn.split('.')
            assert parts[0] == expected_prefix
            return '.'.join(parts[1:])

        # Remove the "model." prefix from FQNs.
        # Context: calling state_dict on an HFAutoModelForCausalLM, will prepend "model." in the
        # state-dict keys. One solution would be to override HFAutoModelForCausalLM's state_dict
        # and `return self.model.state_dict()`, however FSDP2 uses FQNs to acecss modules, therefore
        # removing "model." at the state_dict function level will cause issues with FSDP2.
        keys = list(state_dict.keys())
        io_bytes_state = {}
        for key, new_key in map(lambda x: (x, pop_fqn_prefix(x)), keys):
            val = state_dict.pop(key)
            if isinstance(val, _io.BytesIO):
                io_bytes_state[new_key] = val
            else:
                state_dict[new_key] = val

        if len(io_bytes_state) > 0:
            logging.warning("State-dict contains _io.BytesIO, those will be saved separately to `io_bytes.pt`.")
            torch.save(io_bytes_state, path / 'io_bytes.pt')

        self.model.save_pretrained(path, state_dict=state_dict)
        if self._tokenizer is not None:
            self._tokenizer.save_pretrained(path)
        else:
            logging.warning("A tokenizer wasn't created before to save.")

    def load_pretrained(self, path):
        """
        Used from HFcheckpointio to load a checkpoint
        TODO(@akoumparouli): refactor
        """
        return AutoModelForCausalLM.from_pretrained(
            path,
            torch_dtype='auto',
            device_map="cpu",
            trust_remote_code=self.trust_remote_code,
            load_in_4bit=self.load_in_4bit,
            attn_implementation=self.attn_implementation,
        ).state_dict()

    def load_state_dict(self, state_dict, strict=True, assign=False):
        """Loads the state-dict directly to self.model, therefore FQNs are expected
        not to start with "model." -- referring to HFAutoModelForCausalLM's attribute.

        Args:
            state_dict (dict): a dict containing parameters and
                persistent buffers.
            strict (bool, optional): whether to strictly enforce that the keys
                in :attr:`state_dict` match the keys returned by this module's
                :meth:`~torch.nn.Module.state_dict` function. Default: ``True``
            assign (bool, optional): When set to ``False``, the properties of the tensors
                in the current module are preserved whereas setting it to ``True`` preserves
                properties of the Tensors in the state dict. The only
                exception is the ``requires_grad`` field of :class:`~torch.nn.Parameter`s
                for which the value from the module is preserved.
                Default: ``False``

        Returns:
            ``NamedTuple`` with ``missing_keys`` and ``unexpected_keys`` fields:
                * **missing_keys** is a list of str containing any keys that are expected
                    by this module but missing from the provided ``state_dict``.
                * **unexpected_keys** is a list of str containing the keys that are not
                    expected by this module but present in the provided ``state_dict``.
        """
        self.model.load_state_dict(state_dict, strict=strict, assign=assign)

    def make_checkpoint_io(self, adapter_only=False):
        """
        Creates a checkpoint_io object for this model;
        the main issue is passing self to the HFCheckpointIO, because it needs
        to call save_pretrained within.
        TODO(@akoumparouli): refactor ^
        """
        from nemo.lightning.io.hf import HFCheckpointIO

        return HFCheckpointIO(model=self, adapter_only=adapter_only)

    def _remove_extra_batch_keys(self, batch, reserved_keys=['labels', 'loss_mask']):
        """Remove extra keys from batch that are not kwargs in model's forward

        Args:
            batch (dict): dictionary of tensors.

        Returns:
            dict: dictionary of tensors; keys that are not in model's forward are removed.
        """
        import inspect

        fwd_signature = inspect.signature(self.model.forward)
        allowed_keys = list(fwd_signature.parameters.keys()) + reserved_keys
        return {k: batch[k] for k in allowed_keys if k in batch}
