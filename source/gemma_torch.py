# Copyright 2024 Google LLC
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
# Inference-only Gemma model implementation.
import re
from typing import (
    Any, 
    List, 
    Optional, 
    Sequence, 
    Tuple, 
    Union)
import torch
import torch.nn as nn
import torch.nn.functional as F
import safetensors
from source.config import *
from source.tokenizer import *


def precompute_freqs_cis(dim: int, end: int, theta: float = 10000.0) -> torch.Tensor:
    # Precomputes the frequency cis.
    freqs     = 1.0 / (theta**(torch.arange(0, dim, 2)[:(dim // 2)].float() / dim))
    t         = torch.arange(end, device=freqs.device)
    freqs     = torch.outer(t, freqs).float()
    freqs_cis = torch.polar(torch.ones_like(freqs), freqs)  # complex64
    return freqs_cis


def apply_rotary_emb(x: torch.Tensor, freqs_cis: torch.Tensor) -> torch.Tensor:
    # Applies the rotary embedding to the query and key tensors.
    x_    = torch.view_as_complex(torch.stack(torch.chunk(x.transpose(1, 2).float(), 2, dim=-1), dim=-1))
    x_out = torch.view_as_real(x_ * freqs_cis).type_as(x)
    x_out = torch.cat(torch.chunk(x_out, 2, dim=-1), dim=-2)
    x_out = x_out.reshape(x_out.shape[0], x_out.shape[1], x_out.shape[2], -1).transpose(1, 2)
    return x_out


class Sampler(nn.Module):
    def __init__(self, vocab_size: int):
        super().__init__()
        self.vocab_size = vocab_size

    @torch.no_grad()
    def forward(self,
        embedding: torch.Tensor,
        hidden_states: torch.Tensor,
        output_positions: torch.Tensor,
        temperatures: Union[torch.Tensor, None],
        top_ps: torch.Tensor,
        top_ks: torch.Tensor,
        embedding_bias: Optional[torch.Tensor] = None,
        ) -> torch.Tensor:
        # Select the last element for each sequence.
        # (batch_size, input_len, hidden_size) -> (batch_size, hidden_size)
        # output_position에 해당하는 인덱스 값의 dim = 1을 읽기
        hidden_states = hidden_states.index_select(1, output_positions).squeeze(dim=1)

        # embedding.t()와 matmul하여 256000개의 단어 사전 로짓을 계산
        logits = torch.matmul(hidden_states, embedding.t())
        if embedding_bias is not None:
            logits += embedding_bias

        # temperature가 None이면, 가장 큰 값을 로짓으로 선택
        # 아니면, temperature 스케일링 적용
        if temperatures is None:
            return torch.argmax(logits, dim=-1).squeeze(dim=-1)
        logits.div_(temperatures.unsqueeze(dim=1))

        # 1. 모든 가능한 단어에 대한 모델 예측의 확률 분포를 계산
        # 2. 내림차순으로 정렬, probs_idx는 내림차순한 원소들이 몇 번 인덱스 인지를 반환
        probs = torch.softmax(logits, dim=-1, dtype=torch.float)
        probs_sort, probs_idx = torch.sort(probs, dim=-1, descending=True)

        # 1. 정렬된 확률의 누적합을 계산 -> 모든 값을 더하면 끝에서 1
        # 2. 누적합과 내림차순확률의 차이를 계산 -> 차이가 top_pos보다 큰 것만 선택 
        # 3. 누적확률이 top p에 도달하기 단어만 선택
        # 4. [0.5, 0.3, 0.2] top p = 0.8이면 [0.5, 0.3] 만 선택
        probs_sum   = torch.cumsum(probs_sort, dim=-1)
        top_ps_mask = (probs_sum - probs_sort) > top_ps.unsqueeze(dim=1)
        probs_sort  = torch.where(top_ps_mask, 0, probs_sort) # (boolean, x, y), True이면 x, False이면 y

        # 1. probs_idx 길이만큼의 0 ~ 숫자 텐서 생성
        # 2. top_ks보다 큰 것은 True로 마스킹
        # 3. 마스킹한 위치가 True이면 0, False이면 probs_sort로 하여 선택
        top_ks_mask = torch.arange(probs_idx.shape[-1], device=probs_idx.device)
        top_ks_mask = top_ks_mask.expand(probs_idx.shape[0], -1)
        top_ks_mask = top_ks_mask >= top_ks.unsqueeze(dim=1)
        probs_sort  = torch.where(top_ks_mask, 0, probs_sort)

        # 1. top-p, top-k로 필터링된 probs_sort를 재정규화
        # 2. 필터링된 과정에서 prob_sort를 probs_idx에 따라 재정렬
        probs_sort.div_(probs_sort.sum(dim=-1, keepdim=True))
        probs = torch.gather(probs_sort, dim=-1, index=torch.argsort(probs_idx, dim=-1))
        # 3. multinomial에 따라 probs에서 1개를 선택하여 next_token으로 보냄
        next_token_ids = torch.multinomial(probs, num_samples=1, replacement=True).squeeze(dim=-1)
        return next_token_ids


class Embedding(nn.Module):
    def __init__(self, num_embeddings: int, embedding_dim: int, quant: bool):
        super().__init__()
        """
        1. in_features, out_feature를 받아서 embedding 레이어를 만든다.
        2. Quantization을 한다면, out_feuatre로 weight_scaler를 만들어서 곱한다.
        """
        if quant:
            self.weight = nn.Parameter(
                torch.empty((num_embeddings, embedding_dim), dtype=torch.int8), 
                requires_grad=False
                )
            self.weight_scaler = nn.Parameter(torch.Tensor(num_embeddings))
        else:
            self.weight = nn.Parameter(
                torch.empty((num_embeddings, embedding_dim)), 
                requires_grad=False
                )
        self.quant = quant

    def forward(self, x):
        weight = self.weight
        if self.quant:
            weight = weight * self.weight_scaler.unsqueeze(-1)
        output = F.embedding(x, weight)
        return output


class RMSNorm(torch.nn.Module):
    def __init__(self,
        dim: int,
        eps: float = 1e-6,
        add_unit_offset: bool = True,
        ):
        super().__init__()
        # https://arxiv.org/abs/1910.07467
        self.eps = eps
        self.add_unit_offset = add_unit_offset
        self.weight = nn.Parameter(torch.zeros(dim))

    def _norm(self, x):
        return x * torch.rsqrt(x.pow(2).mean(-1, keepdim=True) + self.eps)

    def forward(self, x):
        x = self._norm(x.float()).type_as(x)

        if self.add_unit_offset:
            output = x * (1 + self.weight)
        else:
            output = x * self.weight
        return output


class Linear(nn.Module):
    def __init__(self, in_features: int, out_features: int, quant: bool):
        super().__init__()
        """
        1. in_features, out_feature를 받아서 MLP 레이어를 만든다.
        2. Quantization을 한다면, out_feuatre로 weight_scaler를 만들어서 곱한다.
        3. nn.Parameters로 torch.empty(~~)를 담는다.
        """
        if quant:
            self.weight = nn.Parameter(
                torch.empty((out_features, in_features), dtype=torch.int8), 
                requires_grad=False
                )
            self.weight_scaler = nn.Parameter(torch.Tensor(out_features))
        else:
            self.weight = nn.Parameter(
                torch.empty((out_features, in_features)), 
                requires_grad=False
                )
        self.quant = quant

    def forward(self, x):
        weight = self.weight
        if self.quant:
            weight = weight * self.weight_scaler.unsqueeze(-1)
        output = F.linear(x, weight)
        return output


class GemmaMLP(nn.Module):
    def __init__(self,
        hidden_size: int,
        intermediate_size: int,
        quant: bool,
        ):
        super().__init__()
        self.gate_proj = Linear(hidden_size, intermediate_size, quant)
        self.up_proj   = Linear(hidden_size, intermediate_size, quant)
        self.down_proj = Linear(intermediate_size, hidden_size, quant)

    def forward(self, x):
        gate    = self.gate_proj(x)
        gate    = F.gelu(gate, approximate="tanh")
        up      = self.up_proj(x)
        fuse    = gate * up
        outputs = self.down_proj(fuse)
        return outputs


class GemmaAttention(nn.Module):
    def __init__(self,
        hidden_size: int,
        num_heads: int,
        num_kv_heads: int,
        head_dim: int,
        quant: bool,
        ):
        super().__init__()
        self.num_heads    = num_heads
        self.num_kv_heads = num_kv_heads
        assert self.num_heads % self.num_kv_heads == 0
        self.num_queries_per_kv = self.num_heads // self.num_kv_heads

        self.hidden_size  = hidden_size
        self.head_dim     = head_dim
        # q 사이즈는 heads 수에 head_dim을 곱함
        # kv 사이즈는 kv heads 수에 head_dim을 곱함
        self.q_size       = self.num_heads * self.head_dim
        self.kv_size      = self.num_kv_heads * self.head_dim
        self.scaling      = self.head_dim**-0.5

        # self.qkv_proj = Linear(
        #     self.hidden_size, (self.num_heads + 2 * self.num_kv_heads) * self.head_dim, quant=quant)
        self.q_proj      = Linear(
            self.hidden_size, (self.num_heads) * self.head_dim, quant=quant)
        self.k_proj      = Linear(
            self.hidden_size, (self.num_kv_heads) * self.head_dim, quant=quant)
        self.v_proj      = Linear(
            self.hidden_size, (self.num_kv_heads) * self.head_dim, quant=quant)
        self.o_proj      = Linear(
            self.num_heads * self.head_dim, self.hidden_size, quant=quant)

    def forward(self,
        hidden_states: torch.Tensor,
        freqs_cis: torch.Tensor,
        kv_write_indices: torch.Tensor,
        kv_cache: Tuple[torch.Tensor, torch.Tensor],
        mask: torch.Tensor,
        ) -> torch.Tensor:
        hidden_states_shape = hidden_states.shape
        assert len(hidden_states_shape) == 3
        
        batch_size, input_len, _ = hidden_states_shape # [B, L, D]
        # qkv = self.qkv_proj(hidden_states)
        # xq, xk, xv = qkv.split([self.q_size, self.kv_size, self.kv_size], dim=-1)
        xq = self.q_proj(hidden_states)
        xk = self.k_proj(hidden_states)
        xv = self.v_proj(hidden_states)
        xq = xq.view(batch_size, -1, self.num_heads, self.head_dim)
        xk = xk.view(batch_size, -1, self.num_kv_heads, self.head_dim)
        xv = xv.view(batch_size, -1, self.num_kv_heads, self.head_dim)

        # Positional embedding.
        xq = apply_rotary_emb(xq, freqs_cis=freqs_cis)
        xk = apply_rotary_emb(xk, freqs_cis=freqs_cis)

        # Write new kv cache.
        # [batch_size, input_len, n_local_kv_heads, head_dim]
        # xk의 kv_write_indices 인덱스에 1로 채우기
        k_cache, v_cache = kv_cache
        k_cache.index_copy_(1, kv_write_indices, xk)
        v_cache.index_copy_(1, kv_write_indices, xv)

        key   = k_cache
        value = v_cache
        if self.num_kv_heads != self.num_heads:
            # [batch_size, max_seq_len, n_local_heads, head_dim]
            key   = torch.repeat_interleave(key, self.num_queries_per_kv, dim = 2)
            value = torch.repeat_interleave(value, self.num_queries_per_kv, dim = 2)

        # [batch_size, n_local_heads, input_len, head_dim]
        q = xq.transpose(1, 2)
        # [batch_size, n_local_heads, max_seq_len, head_dim]
        k = key.transpose(1, 2)
        v = value.transpose(1, 2)

        # [batch_size, n_local_heads, input_len, max_seq_len]
        scores = torch.matmul(q, k.transpose(2, 3)) * self.scaling
        scores = scores + mask
        # 240507: 소프트맥스 연산 전후가 다름
        scores = F.softmax(scores.float(), dim=-1).type_as(q)

        # [batch_size, n_local_heads, input_len, head_dim]
        output = torch.matmul(scores, v)
        # print(v[0][0][0][:5])
        # print(output[0][0][0][:5])

        # [batch_size, input_len, hidden_dim]
        output = (output.transpose(1, 2).contiguous().view(batch_size, input_len, -1))
        output = self.o_proj(output)
        return output


class GemmaDecoderLayer(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.self_attn = GemmaAttention(
            hidden_size=config.hidden_size,
            num_heads=config.num_attention_heads,
            num_kv_heads=config.num_key_value_heads,
            head_dim=config.head_dim,
            quant=config.quant,
            )
        self.mlp = GemmaMLP(
            hidden_size=config.hidden_size,
            intermediate_size=config.intermediate_size,
            quant=config.quant,
            )
        self.input_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(self,
        hidden_states: torch.Tensor,
        freqs_cis: torch.Tensor,
        kv_write_indices: torch.Tensor,
        kv_cache: Tuple[torch.Tensor, torch.Tensor],
        mask: torch.Tensor,
        ) -> torch.Tensor:
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


class GemmaModel(nn.Module):
    def __init__(self, config):
        super().__init__()
        """
        gemma-2b의 경우 num_hidden_layers = 18
        gemma-7b의 경우 num_hidden_layers = 28
        """
        self.config        = config
        self.vocab_size    = config.vocab_size
        self.embed_tokens  = Embedding(self.vocab_size, config.hidden_size, config.quant)
        self.layers = nn.ModuleList()
        for _ in range(config.num_hidden_layers):
            self.layers.append(GemmaDecoderLayer(config))
        self.norm   = RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def forward(self,
        hidden_states: torch.Tensor,
        freqs_cis: torch.Tensor,
        kv_write_indices: torch.Tensor,
        kv_caches: List[Tuple[torch.Tensor, torch.Tensor]],
        mask: torch.Tensor,
        ) -> torch.Tensor:

        # hidden_states, freqs_cis, kv_write_indeices, kv_caches, mask를 입력받아서,
        # gemma-2b는 디코더 레이어 18번 반복
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
            # if i == 0:
            #     break
            
        # 마지막 RMSNorm 레이어 포워드
        hidden_states = self.norm(hidden_states)        
        return hidden_states


class GemmaForCausalLM(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.config = config
        assert config.hidden_size % config.num_attention_heads == 0
        print("dtype :   ", config.dtype)
        max_seq_len    = config.max_position_embeddings
        head_dim       = config.head_dim
        vocab_size     = config.vocab_size
        self.tokenizer     = Tokenizer(config.tokenizer)
        self.model         = GemmaModel(config)
        self.sampler       = Sampler(vocab_size)

        # Pre-compute rotary embedding table.
        rope_theta = getattr(config, 'rope_theta', 10000)
        freqs_cis  = precompute_freqs_cis(head_dim, max_seq_len * 2, theta=rope_theta)
        self.register_buffer('freqs_cis', freqs_cis)

    @torch.no_grad()
    def forward(self,
        input_token_ids: torch.Tensor,
        input_positions: torch.Tensor,
        kv_write_indices: torch.Tensor,
        kv_caches: List[Tuple[torch.Tensor, torch.Tensor]],
        mask: torch.Tensor,
        output_positions: torch.Tensor,
        temperatures: Union[torch.Tensor, None],
        top_ps: torch.Tensor,
        top_ks: torch.Tensor,
        **kwargs,
        ) -> torch.Tensor:
        freqs_cis        = self.freqs_cis.index_select(0, input_positions)
        kv_write_indices = input_positions

        # 프롬프트 아이디를 임베딩: 해당되는 단어 아이디만 2048 차원 벡터로 변환하여 행렬 구성
        # embedder.weight.shape = [batch_size, 256000, 2048]
        hidden_states = self.model.embed_tokens(input_token_ids)
        # hidden_states.shape = [batch_size, input_len, 2048]
        # Gemma normalizes the embedding by sqrt(hidden_size).
        hidden_states = hidden_states * (self.config.hidden_size**0.5) 
        hidden_states = self.model(
            hidden_states=hidden_states,
            freqs_cis=freqs_cis,
            kv_write_indices=kv_write_indices,
            kv_caches=kv_caches,
            mask=mask,
            )
        
        # HC: embedder의 weight를 reuse한다.
        embedder_weight = self.model.embed_tokens.weight
        if self.config.quant:
            embedder_weight = (embedder_weight * self.model.embed_tokens.weight_scaler.unsqueeze(-1))
        next_tokens = self.sampler(
            embedding=embedder_weight,
            hidden_states=hidden_states,
            output_positions=output_positions,
            temperatures=temperatures,
            top_ps=top_ps,
            top_ks=top_ks,
            )
        return next_tokens

    def generate(self,
        prompts: Union[str, Sequence[str]],
        device: Any,
        output_len: int = 100,
        temperature: Union[float, None] = 0.95,
        top_p: float = 1.0,
        top_k: int = 100,
        ) -> Union[str, Sequence[str]]:
        """
        Generates responses for given prompts using Gemma model.
        HC: Mac에서 추론할 것이므로 .to(device)는 모두 제거
        """
        # If a single prompt is provided, treat it as a batch of 1.
        is_str_prompt = isinstance(prompts, str)
        if is_str_prompt:
            prompts = [prompts]

        batch_size     = len(prompts) # 1개의 문장이면 batch_size = 1
        prompt_tokens  = [self.tokenizer.encode(prompt) for prompt in prompts] # 배치의 각 프롬프트트들을 인코딩
        min_prompt_len = min(len(p) for p in prompt_tokens) # 숫자로 표현한 프롬프트들 중 가장 짧은 프롬프트 길이
        max_prompt_len = max(len(p) for p in prompt_tokens) # 숫자로 표현한 프롬프트들 중 가장 긴 프롬프트 길이
        max_seq_len    = max_prompt_len + output_len # 출력 길이는 100
        assert max_seq_len <= self.config.max_position_embeddings


        # KV 캐시 빌드
        # num_hidden_layers 수 많큼 size, dtype 크기의 torch.zeros k, v를 kv_caches에 담기
        kv_caches = []
        for _ in range(self.config.num_hidden_layers):
            size    = (batch_size, max_seq_len, self.config.num_key_value_heads, self.config.head_dim)
            dtype   = self.config.get_dtype()
            k_cache = torch.zeros(size=size, dtype=dtype, device=device)
            v_cache = torch.zeros(size=size, dtype=dtype, device=device)
            kv_caches.append((k_cache, v_cache))


        # HC: 프롬프트를 토크나이징하고, 숫자 아이디로 매핑
        token_ids_tensor        = torch.full((batch_size, max_seq_len), self.tokenizer.pad_id, dtype=torch.int64)
        input_token_ids_tensor  = torch.full((batch_size, min_prompt_len), self.tokenizer.pad_id, dtype=torch.int64)
        for i, p in enumerate(prompt_tokens):
            token_ids_tensor[i, :len(p)] = torch.tensor(p)
            input_token_ids_tensor[i, :min_prompt_len] = torch.tensor(p[:min_prompt_len])

        prompt_mask_tensor      = token_ids_tensor != self.tokenizer.pad_id
        input_positions_tensor  = torch.arange(0, min_prompt_len, dtype=torch.int64) # tensor([0, 1, 2, 3, 4, 5])

        mask_tensor = torch.full((1, 1, max_seq_len, max_seq_len), -2.3819763e38).to(torch.float)
        mask_tensor = torch.triu(mask_tensor, diagonal=1)

        curr_mask_tensor        = mask_tensor.index_select(2, input_positions_tensor)
        output_positions_tensor = torch.LongTensor([min_prompt_len - 1])
        temperatures_tensor = None if not temperature else torch.FloatTensor([temperature] * batch_size)
        top_ps_tensor = torch.FloatTensor([top_p] * batch_size)
        top_ks_tensor = torch.LongTensor([top_k] * batch_size)
        output_index  = torch.tensor(min_prompt_len, dtype=torch.int64)

        # HC: 실제 모델 포워드, 
        # max_sqe_len - min_prompt_len의 의미: 아마도 2개 이상의 배치를 추론할때 토큰 길이를 맞추기 위해서이지 않을까?
        for i in range(max_seq_len - min_prompt_len):
            # 처음에는 입력 프롬프트 전체를 넣고, 입력 프롬프트를 통해 K, V를 연산하여 보관
            # 두 번째부터는 출력 토큰을 다시 입력으로 넣어서 다음 언어를 K, V를 참고하여 예측
            # 각 출력마다 다음 단어를 예측하고 이들을 모아서 하나의 출력 문장을 구성
            next_token_ids = self(
                input_token_ids=input_token_ids_tensor, # tensor([[   2,  651, 6996,  576, 1913,  603]])
                input_positions=input_positions_tensor, # tensor([0, 1, 2, 3, 4, 5])
                kv_write_indices=None, # None
                kv_caches=kv_caches, # torch.zeros의 K, V size를 num_hidden_layer만큼 리스트 선언
                mask=curr_mask_tensor,
                output_positions=output_positions_tensor, # 상수: min_prompt_len - 1
                temperatures=temperatures_tensor, # 상수: 0.95
                top_ps=top_ps_tensor, # tensor([1.])
                top_ks=top_ks_tensor, # tensor([100])
                )

            curr_prompt_mask = prompt_mask_tensor.index_select(1, output_index).squeeze(dim=1)
            curr_token_ids   = token_ids_tensor.index_select(1, output_index).squeeze(dim=1)
            output_token_ids = torch.where(curr_prompt_mask, curr_token_ids, next_token_ids).unsqueeze(dim=1)
            token_ids_tensor.index_copy_(1, output_index, output_token_ids)

            input_token_ids_tensor  = output_token_ids
            input_positions_tensor  = output_index.unsqueeze(dim=-1)
            curr_mask_tensor        = mask_tensor.index_select(2, input_positions_tensor)
            output_positions_tensor = torch.tensor(0, dtype=torch.int64)
            output_index = output_index + 1

            # if i == 0:
            #     break

        # HC: 디토크나이징 과정, token_ids_tensor를 문장으로 치환
        token_ids = token_ids_tensor.tolist()
        results = []
        for i, tokens in enumerate(token_ids):
            trimmed_output = tokens[len(prompt_tokens[i]):len(prompt_tokens[i]) + output_len]
            if self.tokenizer.eos_id in trimmed_output:
                eos_index = trimmed_output.index(self.tokenizer.eos_id)
                trimmed_output = trimmed_output[:eos_index]
            results.append(self.tokenizer.decode(trimmed_output))
            
        # 하나의 문장으로 반환
        return results[0] if is_str_prompt else results

    def load_weights(self, model_path: str):
        model1 = safetensors.safe_open(model_path.format("00001", "00002"), framework="pt")
        model2 = safetensors.safe_open(model_path.format("00002", "00002"), framework="pt")
        # model1, model2의 tensor들을 담음
        safe_tensors = {}
        for key in model1.keys() + model2.keys():
            if key in model1.keys():
                safe_tensors[key] = model1.get_tensor(key).type(torch.float32)
            elif key in model2.keys():
                safe_tensors[key] = model2.get_tensor(key).type(torch.float32)

        self.load_state_dict(safe_tensors, strict=False)