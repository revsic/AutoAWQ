import torch.nn as nn
import tqdm
from typing import List, Tuple
from .base import BaseAWQForCausalLM
from awq.utils.fused_utils import fuse_qkv
from awq.modules.fused.block import MixtralBlock
from awq.modules.fused.model import LlamaLikeModel
from transformers.models.mixtral.modeling_mixtral import (
    MixtralDecoderLayer as OldmixtralDecoderLayer,
    MixtralForCausalLM as OldmixtralForCausalLM
)
from awq.modules.fused.mlp import QuantFusedMLP
from awq.modules.fused.norm import FasterTransformerRMSNorm

class MixtralAWQForCausalLM(BaseAWQForCausalLM):
    layer_type = "MixtralDecoderLayer"
    max_new_tokens_key = "max_position_embeddings"

    @staticmethod
    def fuse_layers(model: OldmixtralForCausalLM):
        fuser = MixtralFuser(model)
        fuser.fuse_transformer()

    @staticmethod
    def get_model_layers(model: OldmixtralForCausalLM):
        return model.model.layers
    
    @staticmethod
    def get_act_for_scaling(module):
        return dict(
            is_scalable=False
        )
    
    @staticmethod
    def move_embed(model: OldmixtralForCausalLM, device: str):
        model.model.embed_tokens = model.model.embed_tokens.to(device)
    
    @staticmethod
    def get_layers_for_scaling(module: OldmixtralDecoderLayer, input_feat, module_kwargs):
        layers = []

        # attention input
        layers.append(dict(
            prev_op=module.input_layernorm,
            layers=[module.self_attn.q_proj,
                    module.self_attn.k_proj, module.self_attn.v_proj],
            inp=input_feat['self_attn.q_proj'],
            module2inspect=module.self_attn, kwargs=module_kwargs,
        ))

        # attention out
        if module.self_attn.v_proj.weight.shape == module.self_attn.o_proj.weight.shape:
            layers.append(dict(
                prev_op=module.self_attn.v_proj,
                layers=[module.self_attn.o_proj],
                inp=input_feat['self_attn.o_proj'],
            ))
        
        # expert
        for i, expert in enumerate(module.block_sparse_moe.experts):
            layers.append(dict(
                prev_op=module.post_attention_layernorm,
                layers=[
                    expert.w1,
                    expert.w3
                ],
                inp=input_feat[f"block_sparse_moe.experts.{i}.w1"],
                module2inspect=expert,
                kwargs={"routing_weights": input_feat[f"block_sparse_moe.experts.{i}.routing_weights"]}
            ))
            layers.append(dict(
                prev_op=expert.w3,
                layers=[expert.w2],
                inp=input_feat[f"block_sparse_moe.experts.{i}.w2"],\
            ))

        return layers


class MixtralMLP(nn.Module):
    def __init__(self, gate_proj, down_proj, up_proj):
        super().__init__()
        self.fused_mlp = QuantFusedMLP(
            gate_proj=gate_proj,
            down_proj=down_proj,
            up_proj=up_proj)
    
    def forward(self, hidden_states, routing_weights):
        return routing_weights * self.fused_mlp(hidden_states)


class MixtralFuser:
    def __init__(self, model: OldmixtralForCausalLM):
        self.model = model

        self.mixtral_blocks: List[Tuple[str, OldmixtralDecoderLayer]] = [
            (name, module) for name, module in self.model.named_modules()
            if 'MixtralDecoderLayer'.lower() in module.__class__.__name__.lower()
        ]
    
    def fuse_transformer(self):
        blocks = []

        module: OldmixtralDecoderLayer
        for module in tqdm.tqdm(self.model.model.layers, desc="Fusing layers..."):
            device = next(iter(module.state_dict().values())).device
            qkv = fuse_qkv(
                module,
                module.self_attn.q_proj,
                module.self_attn.k_proj,
                module.self_attn.v_proj
            )
            # Adapt to mixture of experts
            for i in range(len(module.block_sparse_moe.experts)):
                mlp = MixtralMLP(
                    gate_proj=module.block_sparse_moe.experts[i].w1,
                    down_proj=module.block_sparse_moe.experts[i].w2,
                    up_proj=module.block_sparse_moe.experts[i].w3
                )
                module.block_sparse_moe.experts[i] = mlp
            norm_1 = FasterTransformerRMSNorm(
                module.input_layernorm.weight,
                module.input_layernorm.variance_epsilon
            )
            norm_2 = FasterTransformerRMSNorm(
                module.post_attention_layernorm.weight,
                module.post_attention_layernorm.variance_epsilon
            )
            blocks.append(MixtralBlock(
                hidden_size=self.model.config.hidden_size,
                n_heads=self.model.config.num_attention_heads,
                n_kv_heads=self.model.config.num_key_value_heads,
                qkv_layer=qkv,
                o_proj=module.self_attn.o_proj,
                moe=module.block_sparse_moe,
                norm_1=norm_1,
                norm_2=norm_2,
                dev=device,
                max_seq_len=self.model.config.max_new_tokens
            ))

        self.model.model = LlamaLikeModel(
            self.model.config.vocab_size,
            blocks,
            self.model.model.embed_tokens,
            self.model.model.norm,
        )
