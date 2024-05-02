"""
Inference-only Gemma model implementation for MLX
"""

import re
from typing import Any, List, Optional, Sequence, Tuple, Union
import numpy as np
import mlx
import mlx.nn as mx
import mlx.core as mxc
from source.gemma import config
from source.gemma import tokenizer


def MLXprecompute_freqs_cis(dim: int, end: int, theta: float = 10000.0) -> mxc.array:
    """
    Precomputes the frequencey cis.
    """
    freqs = 1.0 / (theta ** (mxc.arange(0, dim, 2)[:(dim // 2)].astype(mxc.float32) / dim))
    t = mxc.arange(end)
    freqs = mxc.outer(t, freqs)
    cos = mxc.ones_like(freqs) * mxc.cos(freqs)
    sin = mxc.ones_like(freqs) * mxc.sin(freqs)
    freq_cis = mxc.stack([cos, sin], axis = -1)
    return freq_cis


def MLXapply_rotary_emb(x: mxc.array, freqs_cis: mxc.array) -> mxc.array:
    x_transpose = x.transpose(0, 2, 1, 3).astype(mxc.float32) # step 1
    x_real = x_transpose[:, :, :, :x_transpose.shape[3]//2] # step 2
    x_imag = x_transpose[:, :, :, x_transpose.shape[3]//2:]
    x_     = mxc.stack([x_real, x_imag], axis = -1) # step 3 ~ step4

    x_out_real = x_[:, :, :, :, 0] * freqs_cis[:, :, 0] - x_[:, :, :, :, 1] * freqs_cis[:, :, 1] # step 5
    x_out_imag = x_[:, :, :, :, 1] * freqs_cis[:, :, 0] + x_[:, :, :, :, 0] * freqs_cis[:, :, 1]
    x_out = mxc.stack([x_out_real, x_out_imag], axis = -1)

    # 해결해야 할 부분
    x_out__real = x_out[:, :, :, :, 0][:, :, :, :, None]
    x_out__imag = x_out[:, :, :, :, 1][:, :, :, :, None]
    x_out = mxc.concatenate([x_out__real, x_out__imag], axis = 3)
    x_out = mxc.reshape(x_out, (x_out.shape[0], x_out.shape[1], x_out.shape[2], -1)).transpose(0, 2, 1, 3)
    return x_out
    

class MLXSampler(mx.Module):
    def __init__(self, vocab_size: int):
        super().__init__()
        self.vocab_size = vocab_size

    # @torch.no_grad()
    def __call__(self,
        embedding: mxc.array,
        hidden_states: mxc.array,
        output_positions: mxc.array,
        temperatures: Union[mxc.array, None],
        top_ps: mxc.array,
        top_ks: mxc.array,
        embedding_bias: Optional[mxc.array] = None,
        ) -> mxc.array:

        # Select the last element for each sequence.
        # (batch_size, input_len, hidden_size) -> (batch_size, hidden_size)
        # output_position에 해당하는 인덱스 값의 dim = 1을 읽기
        hidden_states = hidden_states[:, output_positions, :].squeeze(axis=1)
        # embedding.t()와 matmul하여 256000개의 단어 사전 로짓을 계산
        logits = mxc.matmul(hidden_states, embedding.T)
        if embedding_bias is not None:
            logits += embedding_bias

        # temperature가 None이면, 가장 큰 값을 로짓으로 선택
        # 아니면, temperature 스케일링 적용
        if temperatures is None:
            return mxc.argmax(logits, dim=-1).squeeze(dim=-1)
        logits = logits / (temperatures[None, :])

        # 1. 모든 가능한 단어에 대한 모델 예측의 확률 분포를 계산
        # 2. 내림차순으로 정렬, probs_idx는 내림차순한 원소들이 몇 번 인덱스 인지를 반환
        probs = mxc.softmax(logits, axis=-1)
        probs_sort = mxc.sort(probs, axis = -1)
        probs_idx  = mxc.argsort(probs, axis = -1)
        
        ## 오름차순으로 정렬되어있어서 이 기준으로 연산
        # 1. 정렬된 확률의 누적합을 계산 -> 모든 값을 더하면 끝에서 1
        # 2. 누적합과 내림차순확률의 차이를 계산 -> 차이가 top_pos보다 큰 것만 선택 
        # 3. 누적확률이 top p에 도달하기 단어만 선택
        # 4. [0.5, 0.3, 0.2] top p = 0.8이면 [0.5, 0.3] 만 선택
        probs_sum   = mxc.cumsum(probs_sort, axis = -1, reverse = True)
        top_ps_mask = (probs_sum - probs_sort) > top_ps[None, :]
        probs_sort  = mxc.where(top_ps_mask, 0, probs_sort)

        # 1. probs_idx 길이만큼의 0 ~ 숫자 텐서 생성
        # 2. top_ks보다 큰 것은 True로 마스킹
        # 3. 마스킹한 위치가 True이면 0, False이면 probs_sort로 하여 선택
        top_ks_mask = mxc.arange(probs_idx.shape[-1])
        top_ks_mask = top_ks_mask[None, :]
        # top_ks_mask = top_ks_mask >= top_ks[None, :] 오름차순으로 사용하기 때문에 주석 처리
        top_ks_mask = top_ks_mask < top_ks[None, :]
        probs_sort  = mxc.where(top_ks_mask, 0, probs_sort)

        # 1. top-p, top-k로 필터링된 probs_sort를 재정규화
        # 2. 필터링된 과정에서 prob_sort를 probs_idx에 따라 재정렬
        probs_sort = probs_sort / probs_sort.sum(axis = -1, keepdims = True)

        # MLX의 mxc.multinomial을 구현해보자.
        # probs = mxc.gather(probs_sort, dim=-1, index=mxc.argsort(probs_idx, dim=-1)) # 오름차순으로 사용하기 때문에 주석 처리
        np_probs = np.array(probs).squeeze(0)
        next_token_ids = np.random.choice(len(probs[0]), size = 1, replace = True, p = np_probs)
        return next_token_ids
    

class MLXEmbedding(mx.Module):
    def __init__(self, num_embeddings: int, embedding_dim: int, quant: bool):
        super().__init__()
        """
        1. in_features, out_feature를 받아서 embedding 레이어를 만든다.
        2. Quantization을 한다면, out_feuatre로 weight_scaler를 만들어서 곱한다.
        """
        if quant:
            self.embedding     = mx.Embedding(num_embeddings, embedding_dim)
            self.weight_scaler = mxc.zeros(shape = [num_embeddings])
        else:
            self.embedding     = mx.Embedding(num_embeddings, embedding_dim)
        self.quant = quant
    
    def __call__(self, x):
        if self.quant:
            self.embedding.weight = self.embedding.weight * self.weight_scaler[ :, None]
        output = self.embedding(x)
        return output
    

class MLXRMSNorm(mx.Module):
    def __init__(
        self,
        dim: int,
        eps: float = 1e-6,
        add_unit_offset: bool = True,
        ):
        super().__init__()
        """
        https://arxiv.org/abs/1910.07467
        """
        self.eps = eps
        self.add_unit_offset = add_unit_offset
        self.weight = mxc.zeros(shape = [dim])

    def _norm(self, x):
        x_ = x ** 2
        x_ = x_.mean(-1, keepdims = True)
        return x * mxc.rsqrt(x_ + self.eps)

    def __call__(self, x):
        x = x.astype(mxc.float32)
        x = self._norm(x).astype(x.dtype)

        if self.add_unit_offset:
            output = x * (1 + self.weight)
        else:
            output = x * self.weight
        return output
    

class MLXLinear(mx.Module):
    def __init__(self, in_features: int, out_features: int, quant: bool):
        super().__init__()
        """
        torch: [in_dim, out_dim] @ we
        1. in_features, out_feature를 받아서 MLP 레이어를 만든다.
        2. Quantization을 한다면, out_feuatre로 weight_scaler를 만들어서 곱한다.
        """
        if quant:
            self.weight        = mxc.zeros(shape = [in_features, out_features], dtype = mxc.int8)
            self.weight_scaler = mxc.zeros(shape = [out_features])
        else:
            self.weight        = mxc.zeros(shape = [in_features, out_features], dtype = mxc.int8)
        self.quant = quant

    def __call__(self, x):
        weight = self.weight
        if self.quant:
            weight = weight * self.weight_scaler[:, None]
        output = x @ weight.T
        return output


class MLXGemmaMLP(mx.Module):
    def __init__(
        self,
        hidden_size: int,
        intermediate_size: int,
        quant: bool,
        ):
        super().__init__()
        self.phi = mxc.array(np.pi)
        self.gate_proj = MLXLinear(hidden_size, intermediate_size, quant)
        self.up_proj   = MLXLinear(hidden_size, intermediate_size, quant)
        self.down_proj = MLXLinear(intermediate_size, hidden_size, quant)

    def gelu_appro_tanh(self, x):
        output = 0.5 * x * (1 + mxc.tanh(mxc.sqrt(2/self.phi) * (x + 0.044715 * (x ** 3))))
        return output
        
    def __call__(self, x):
        gate = self.gate_proj(x)
        gate = self.gelu_appro_tanh(gate)
        up   = self.up_proj(x)
        fuse = gate * up
        outputs = self.down_proj(fuse)
        return outputs
    

## HC: MLXGemmaAttention을 수정해야함
class MLXGemmaAttention(mx.Module):
    def __init__(self, hidden_size: int, num_heads: int, num_kv_heads: int, head_dim: int, quant: bool):
        super().__init__()
        self.num_heads    = num_heads
        self.num_kv_heads = num_kv_heads
        assert self.num_heads % self.num_kv_heads == 0
        self.num_queries_per_kv = self.num_heads // self.num_kv_heads

        self.hidden_size = hidden_size
        self.head_dim    = head_dim

        self.q_size      = self.num_heads * self.head_dim
        self.kv_size     = self.num_kv_heads * self.head_dim
        self.scaling     = self.head_dim**-0.5
        self.qkv_proj    = MLXLinear(
            self.hidden_size, (self.num_heads + 2 * self.num_kv_heads) * self.head_dim, quant=quant)
        self.o_proj      = MLXLinear(
            self.num_heads * self.head_dim, self.hidden_size, quant=quant)

    def __call__(self, 
        hidden_states: mxc.array, 
        freqs_cis: mxc.array,
        kv_write_indices: mxc.array,
        kv_cache: Tuple[mxc.array, mxc.array], 
        mask: mxc.array) -> mxc.array:
        hidden_states_shape = hidden_states.shape
        assert len(hidden_states_shape) == 3

        batch_size, input_len, _ = hidden_states_shape
        qkv = self.qkv_proj(hidden_states)
        xq = qkv[:, :, :self.q_size]
        xk = qkv[:, :, self.q_size:self.q_size + self.kv_size]
        xv = qkv[:, :, self.q_size+self.kv_size:]
        xq = xq.reshape(batch_size, -1, self.num_heads, self.head_dim)
        xk = xk.reshape(batch_size, -1, self.num_kv_heads, self.head_dim)
        xv = xv.reshape(batch_size, -1, self.num_kv_heads, self.head_dim)

        # Positional embedding.
        xq = MLXapply_rotary_emb(xq, freqs_cis=freqs_cis)
        xk = MLXapply_rotary_emb(xk, freqs_cis=freqs_cis)

        # Write new kv cache.
        # [batch_size, input_len, n_local_kv_heads, head_dim]
        # cache의 kv_write_indices 인덱스를 xk의 kv_write_indices로 채우기
        k_cache, v_cache = kv_cache
        k_cache[:, kv_write_indices, ...] = xk[:, kv_write_indices, ...]
        v_cache[:, kv_write_indices, ...] = xv[:, kv_write_indices, ...]
    
        key   = k_cache
        value = v_cache
        if self.num_kv_heads != self.num_heads:
            # [batch_size, max_seq_len, n_local_heads, head_dim]
            key   = mxc.repeat(key, self.num_queries_per_kv, axis = 2)
            value = mxc.repeat(value, self.num_queries_per_kv, axis = 2)

        # [batch_size, n_local_heads, input_len, head_dim]
        q = xq.transpose(0, 2, 1, 3)
        # [batch_size, n_local_heads, max_seq_len, head_dim]
        k = key.transpose(0, 2, 1, 3)
        v = value.transpose(0, 2, 1, 3)

        # [batch_size, n_local_heads, input_len, max_seq_len]
        scores = mxc.matmul(q, k.transpose(0, 1, 3, 2)) * self.scaling
        scores = scores + mask
        scores = mxc.softmax(scores.astype(mxc.float32), axis=-1).astype(q.dtype)

        # [batch_size, n_local_heads, input_len, head_dim]
        output = mxc.matmul(scores, v)

        # [batch_size, input_len, hidden_dim]
        # output = (output.transpose(0, 2, 1, 3).contiguous().view(batch_size, input_len, -1))
        output = output.transpose(0, 2, 1, 3).reshape(batch_size, input_len, -1)
        output = self.o_proj(output)
        return output


class MLXGemmaDecoderLayer(mx.Module):
    def __init__(self, config):
        super().__init__()
        self.self_attn = MLXGemmaAttention(
            hidden_size=config.hidden_size,
            num_heads=config.num_attention_heads,
            num_kv_heads=config.num_key_value_heads,
            head_dim=config.head_dim,
            quant=config.quant,
            )
        self.mlp = MLXGemmaMLP(
            hidden_size=config.hidden_size, intermediate_size=config.intermediate_size, quant=config.quant)
        self.input_layernorm          = MLXRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = MLXRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def __call__(self,
        hidden_states: mxc.array,
        freqs_cis: mxc.array,
        kv_write_indices: mxc.array,
        kv_cache: Tuple[mxc.array, mxc.array],
        mask: mxc.array,
        ) -> mxc.array:
        """
        1. hidden -> RMSNorm -> GemmaAttention = hidden + residual
        2. hidden -> RMXNorm -> GemmaMLP -> hidden + residual
        """
        # Self Attention
        residual = hidden_states
        hidden_states = self.input_layernorm(hidden_states)
        hidden_states = self.self_attn(
            hidden_states=hidden_states,
            freqs_cis=freqs_cis,
            kv_write_indices=kv_write_indices,
            kv_cache=kv_cache,
            mask=mask,
            )
        hidden_states = residual + hidden_states

        # MLP
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states
        return hidden_states


class MLXGemmaModel(mx.Module):
    def __init__(self, config):
        super().__init__()
        """
        gemma-2b의 경우 num_hidden_layers = 18
        gemma-7b의 경우 num_hidden_layers = 28
        """
        self.config     = config
        self.vocab_size = config.vocab_size

        self.layers = []
        for _ in range(config.num_hidden_layers):
            self.layers.append(MLXGemmaDecoderLayer(config))
        self.norm   = MLXRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def __call__(self,
        hidden_states: mxc.array,
        freqs_cis: mxc.array,
        kv_write_indices: mxc.array,
        kv_caches: List[Tuple[mxc.array, mxc.array]],
        mask: mxc.array,
        ) -> mxc.array:

        for i in range(len(self.layers)):
            # nn.ModuleList의 GemmaDecoderLayer를 순회
            layer = self.layers[i]
            hidden_states = layer(
                hidden_states=hidden_states,
                freqs_cis=freqs_cis,
                kv_write_indices=kv_write_indices,
                kv_cache=kv_caches[i],
                mask=mask,
                )
            
        # 마지막 RMSNorm 레이어 포워드
        hidden_states = self.norm(hidden_states)
        return hidden_states
    

# class MLXGemmaForCausalLM(mx.Module):
#     def __init__(self, config):
#         super().__init__()
#         self.config = config
#         assert config.hidden_size % config.num_attention_heads == 0
#         print("dtype :   ", config.dtype)
#         max_seq_len    = config.max_position_embeddings
#         head_dim       = config.head_dim
#         vocab_size     = config.vocab_size
#         self.tokenizer = tokenizer.Tokenizer(config.tokenizer)
#         self.embedder  = MLXEmbedding(vocab_size, config.hidden_size, config.quant)
#         self.model     = MLXGemmaModel(config)
#         self.sampler   = MLXSampler(vocab_size)

#         rope_theta = getattr(config, "rope_theta", 10000)
#         self.freqs_cis = MLXprecompute_freqs_cis(head_dim, max_seq_len * 2, theta = rope_theta)


#     def __call__(self,
#         input_token_ids: mxc.array,
#         input_positions: mxc.array,
#         kv_write_indices: mxc.array,
#         kv_caches: List[Tuple[mxc.array, mxc.array]],
#         mask: mxc.array,
#         output_positions: mxc.array,
#         temperatures: Union[mxc.array, None],
#         top_ps: mxc.array,
#         top_ks: mxc.array,
#         **kwargs,
#         ) -> mxc.array:
#         freqs_cis        = self.freqs_cis.index_select(0, input_positions)
#         kv_write_indices = input_positions