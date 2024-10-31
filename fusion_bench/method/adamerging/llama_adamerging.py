import logging
import os
from pathlib import Path
from typing import Optional, Tuple, Union, cast

import lightning as L
import torch
from torch import Tensor, nn
from torch.utils.data import DataLoader
from tqdm.auto import tqdm
from transformers.data.data_collator import (
    DataCollatorForLanguageModeling,
    default_data_collator,
)

from fusion_bench import BaseAlgorithm
from fusion_bench.method.simple_average import simple_average
from fusion_bench.mixins import LightningFabricMixin, SimpleProfilerMixin
from fusion_bench.modelpool import CausalLMPool
from fusion_bench.models.wrappers.layer_wise_fusion import (
    LayerWiseMergedModel,
    get_layer_wise_weights,
    merge_and_unload,
    merge_weights,
    fix_other_parts,
)
from fusion_bench.utils import instantiate
from fusion_bench.utils.data import InfiniteDataLoader, load_tensor_from_file
from fusion_bench.utils.dtype import get_dtype
from fusion_bench.utils.parameters import print_parameters

log = logging.getLogger(__name__)


class LayerWiseAdaMergingForLlamaSFT(
    BaseAlgorithm,
    LightningFabricMixin,
    SimpleProfilerMixin,
):

    modelpool: CausalLMPool

    def __init__(
        self,
        seed: int,
        output_dir: str,
        optimizer: str,
        lr: float,
        sparsity_ratio: float,
        average_attntion: bool,
        start_layer_idx: Optional[Union[float, int]],
        init_values: float,
        init_weights_path: str,
        clamp_weights: bool,
        max_steps: int,
        dataloader_kwargs: bool,
        tie_weights: bool,
        strict: bool,
        skip_training: bool = False,
        save_interval: int = None,
        save_merged_model: bool = True,
        **kwargs,
    ):
        """
        Args:
            seed (int): random seed to set at the begining of running.
        """
        self.seed = seed
        self.output_dir = output_dir
        self.optimizer = optimizer
        self.lr = lr
        self.sparsity_ratio = sparsity_ratio
        self.average_attntion = average_attntion
        self.start_layer_idx = start_layer_idx
        self.init_values = init_values
        self.init_weights_path = init_weights_path
        self.clamp_weights = clamp_weights
        self.max_steps = max_steps
        self.tie_weights = tie_weights
        self.strict = strict
        self.dataloader_kwargs = dataloader_kwargs
        self.skip_training = skip_training
        self.save_interval = save_interval
        self.save_merged_model = save_merged_model
        super().__init__(**kwargs)

    def run(self, modelpool: CausalLMPool):
        self.modelpool = modelpool
        fabric = self.fabric

        assert (
            modelpool.has_pretrained
        ), "Must be a pre-tarined model with name `_pretrained_` in the model pool."
        log.info(f"There are {len(modelpool)} expert models in the model pool.")

        fabric.seed_everything(self.seed)

        if self.output_dir is None:
            log.warning(
                f"`output_dir` is not specified, set to log directory {self.log_dir}."
            )
            self.output_dir = fabric.logger.log_dir
        if fabric.global_rank == 0:
            os.makedirs(self.output_dir, exist_ok=True)

        with self.profile("construct_layer_wise_merged_model"):
            module = self.construct_layer_wise_merged_model(modelpool)
            print_parameters(module)

        if not self.skip_training:
            module = self.train(module)

        model = merge_and_unload(module)
        if self.save_merged_model:
            merged_model_path = os.path.join(
                self.output_dir, "checkpoints", "merged_model"
            )
            if self.fabric.global_rank == 0:
                modelpool.load_tokenizer().save_pretrained(merged_model_path)
                model.save_pretrained(merged_model_path)
                print_parameters(model)
        return model

    @torch.no_grad()
    def construct_layer_wise_merged_model(self, modelpool: CausalLMPool):
        """
        Constructs a wrapped layer-wise merged model from model pool.

        This method creates a new wrapped model by merging the layers of a pretrained model with those of several fine-tuned models.
        The merging is controlled by layer-wise weights, which is a `torch.Tensor` of the shape `(num_models, num_layers)`.
        The merging weights can be initialized based on a provided configuration or loaded from a file.

        Args:
            modelpool (ModelPool): An object containing the pretrained model and fine-tuned models to be merged.

        Returns:
            LayerWiseMergedModel: An instance of the merged model with layer-wise weights applied.
        """
        pretrained_causal_lm = modelpool.load_model("_pretrained_")

        # we only merge the backbone
        pretrained_model = pretrained_causal_lm.model.layers
        finetuned_models = [
            modelpool.load_model(name).model.layers for name in modelpool.model_names
        ]

        if self.start_layer_idx is not None and isinstance(self.start_layer_idx, float):
            self.start_layer_idx = int(self.start_layer_idx * len(pretrained_model))

        if self.start_layer_idx is not None:
            for layer_idx, layer in enumerate(pretrained_model[: self.start_layer_idx]):
                pretrained_model[layer_idx] = simple_average(
                    [m[layer_idx] for m in finetuned_models],
                    base_module=pretrained_model[layer_idx],
                )
                pretrained_model[layer_idx].requires_grad_(False)

        if self.average_attntion:
            for layer_idx, layer in enumerate(pretrained_model):
                if layer_idx < self.start_layer_idx:
                    continue
                layer.self_attn = simple_average(
                    [m[layer_idx].self_attn for m in finetuned_models],
                    base_module=layer.self_attn,
                )
                layer.self_attn.requires_grad_(False)

        # initialize layer-wise weights using the provided configuration `init_values` or load from file if `weights` is provided
        for layer_idx, layer in enumerate(pretrained_model):
            if layer_idx < self.start_layer_idx:
                continue
            layer_wise_weight = get_layer_wise_weights(
                num_models=len(modelpool.model_names),
                num_layers=len(
                    tuple(filter(lambda p: p.requires_grad, layer.parameters()))
                ),
                init_values=self.init_values,
                dtype=get_dtype(layer),
            )

            module = LayerWiseMergedModel(
                layer_wise_weight=layer_wise_weight,
                pretrained_model=pretrained_model[layer_idx],
                finetuned_models=[m[layer_idx] for m in finetuned_models],
                clamp_weights=self.clamp_weights,
                tie_weights=self.tie_weights,
                strict=self.strict,
                sparsity_ratio=self.sparsity_ratio,
            )

            pretrained_causal_lm.model.layers[layer_idx] = module

        fix_other_parts(pretrained_causal_lm)
        return pretrained_causal_lm

    def configure_optimizer(self, module: nn.Module):
        if self.optimizer == "adam":
            optimizer = torch.optim.Adam(
                [p for p in module.parameters() if p.requires_grad], lr=self.lr
            )
            return {"optimizer": optimizer}
        else:
            raise ValueError(f"Unknown optmizer type {self.optimizer}")

    def train(self, causal_lm):
        fabric = self.fabric
        modelpool = self.modelpool

        with self.profile("load datasets and setup dataloaders"):
            train_datasets = {
                dataset_name: modelpool.load_train_dataset(dataset_name)
                for dataset_name in modelpool.train_dataset_names
            }
            train_loaders = {
                dataset_name: fabric.setup_dataloaders(
                    DataLoader(
                        dataset,
                        **self.dataloader_kwargs,
                        collate_fn=default_data_collator,
                    )
                )
                for dataset_name, dataset in train_datasets.items()
            }
            train_loader_iters = {
                dataset_name: iter(InfiniteDataLoader(loader))
                for dataset_name, loader in train_loaders.items()
            }

        optimizer = self.configure_optimizer(causal_lm)["optimizer"]
        causal_lm, optimizer = cast(
            Tuple[nn.Module, torch.optim.Optimizer],
            fabric.setup(causal_lm, optimizer),
        )

        causal_lm.train()
        merge_weights(causal_lm)

        self.save_state("init", causal_lm)

        assert len(train_datasets) > 0, "No training datasets are provided."
        for step_idx in tqdm(range(self.max_steps)):
            losses = []
            for dataset_name, dataloader in train_loader_iters.items():
                # compute loss
                inputs = next(dataloader)
                outputs = causal_lm(**inputs)

                losses.append(outputs.loss)

            if len(losses) > 1:
                total_loss = sum(losses)
            else:
                total_loss = losses[0]

            fabric.backward(total_loss)
            optimizer.step()
            optimizer.zero_grad()

            if (
                self.save_interval is not None
                and (step_idx + 1) % self.save_interval == 0
            ):
                self.save_state(step_idx=step_idx, causal_lm=causal_lm)

            merge_weights(causal_lm)

        self.save_state("latest", causal_lm)

        return causal_lm

    def save_state(self, step_idx: Union[int, str], causal_lm):
        """
        Save merging weights of each layers.
        """
        state = {}
        for layer_idx, layer in enumerate(causal_lm.model.layers):
            if isinstance(layer, LayerWiseMergedModel):
                state[f"layer_{layer_idx}"] = layer.merge_weight

        os.makedirs(os.path.join(self.output_dir, "checkpoints"), exist_ok=True)
        save_path = os.path.join(
            self.output_dir, "checkpoints", f"merging-weights_{step_idx}.ckpt"
        )
        log.info(f"Saving merging weights to {save_path}")
        self.fabric.save(save_path, state)
