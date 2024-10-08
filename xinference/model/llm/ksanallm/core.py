# Copyright 2022-2024 XProbe Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import json
import logging
import time
import uuid
from typing import AsyncGenerator, Dict, List, Optional, TypedDict, Union

from ....types import (
    ChatCompletion,
    ChatCompletionChunk,
    Completion,
    CompletionChoice,
    CompletionChunk,
    CompletionUsage,
)
from .. import LLM, LLMFamilyV1, LLMSpecV1
from ..llm_family import CustomLLMFamilyV1
from ..utils import ChatModelMixin, generate_completion_chunk

logger = logging.getLogger(__name__)


class KSANAModelConfig(TypedDict, total=False):
    tokenizer_mode: str
    trust_remote_code: bool
    tp_size: int
    mem_fraction_static: float
    log_level: str
    attention_reduce_in_fp32: bool  # For gemma


class KSANAGenerateConfig(TypedDict, total=False):
    presence_penalty: float
    frequency_penalty: float
    temperature: float
    top_p: float
    top_k: int
    max_new_tokens: int
    stop: Optional[Union[str, List[str]]]
    ignore_eos: bool
    stream: bool
    stream_options: Optional[Union[dict, None]]


try:
    import ksana_llm  # noqa: F401

    KSANA_INSTALLED = True
except ImportError:
    KSANA_INSTALLED = False

KSANA_SUPPORTED_MODELS = [
    "llama-2",
    "llama-3",
    "llama-3.1",
    "mistral-v0.1",
    "mixtral-v0.1",
    "qwen2.5",
    "qwen2.5-coder",
]
KSANA_SUPPORTED_CHAT_MODELS = [
    "llama-2-chat",
    "llama-3-instruct",
    "llama-3.1-instruct",
    "qwen-chat",
    "qwen1.5-chat",
    "qwen2-instruct",
    "qwen2-moe-instruct",
    "mistral-instruct-v0.1",
    "mistral-instruct-v0.2",
    "mixtral-instruct-v0.1",
    "gemma-it",
    "gemma-2-it",
    "deepseek-v2.5",
    "deepseek-v2-chat",
    "deepseek-v2-chat-0628",
    "qwen2.5-instruct",
    "qwen2.5-coder-instruct",
]


class KSANAModel(LLM):
    def __init__(
        self,
        model_uid: str,
        model_family: "LLMFamilyV1",
        model_spec: "LLMSpecV1",
        quantization: str,
        model_path: str,
        model_config: Optional[KSANAModelConfig],
    ):
        super().__init__(model_uid, model_family, model_spec, quantization, model_path)
        self._model_config = model_config
        self._engine = None

    def load(self):
        try:
            import ksana_llm as ksn
        except ImportError:
            error_message = "Failed to import module 'ksana_llm'"
            installation_guide = [
                "Please make sure 'ksana_llm' is installed. ",
                "You can install it by `pip install 'ksana_llm[all]'`\n",
            ]

            raise ImportError(f"{error_message}\n\n{''.join(installation_guide)}")

        self._model_config = self._sanitize_model_config(self._model_config)

#        # Fix: GH#2169
#        if sgl.__version__ >= "0.2.14":
#            self._model_config.setdefault("triton_attention_reduce_in_fp32", False)
#        else:
#            self._model_config.setdefault("attention_reduce_in_fp32", False)

        logger.info(
            f"Loading {self.model_uid} with following model config: {self._model_config}"
        )

        self._engine = ksn.LlmRuntime(
            model_path=self.model_path,
            tokenizer_path=self.model_path,
            **self._model_config,
        )

    def stop(self):
        logger.info("Stopping KSANA engine")
        self._engine.shutdown()

    def _sanitize_model_config(
        self, model_config: Optional[KSANAModelConfig]
    ) -> KSANAModelConfig:
        if model_config is None:
            model_config = KSANAModelConfig()

        cuda_count = self._get_cuda_count()
        model_config.setdefault("tokenizer_mode", "auto")
        model_config.setdefault("trust_remote_code", True)
        model_config.setdefault("tp_size", cuda_count)
        # See https://github.com/pcg-mlp/KsanaLLM/blob/main/src/ksana_llm/python/serving_server.py
        mem_fraction_static = model_config.get("mem_fraction_static")
        if mem_fraction_static is None:
            tp_size = model_config.get("tp_size", cuda_count)
            if tp_size >= 16:
                model_config["mem_fraction_static"] = 0.79
            elif tp_size >= 8:
                model_config["mem_fraction_static"] = 0.83
            elif tp_size >= 4:
                model_config["mem_fraction_static"] = 0.85
            elif tp_size >= 2:
                model_config["mem_fraction_static"] = 0.87
            else:
                model_config["mem_fraction_static"] = 0.88
        model_config.setdefault("log_level", "info")

        return model_config

    @staticmethod
    def _sanitize_generate_config(
        generate_config: Optional[KSANAGenerateConfig] = None,
    ) -> KSANAGenerateConfig:
        if generate_config is None:
            generate_config = KSANAGenerateConfig()

        generate_config.setdefault("presence_penalty", 0.0)
        generate_config.setdefault("frequency_penalty", 0.0)
        generate_config.setdefault("temperature", 1.0)
        generate_config.setdefault("top_p", 1.0)
        generate_config.setdefault("top_k", -1)
        # See https://github.com/pcg-mlp/KsanaLLM/blob/main/src/ksana_llm/python/serving_server.py
        # 16 is too less, so here set 256 by default
        generate_config.setdefault(
            "max_new_tokens", generate_config.pop("max_tokens", 256)  # type: ignore
        )
        generate_config.setdefault("stop", [])
        generate_config.setdefault("stream", False)
        stream_options = generate_config.get("stream_options")
        generate_config.setdefault("stream_options", stream_options)
        generate_config.setdefault("ignore_eos", False)

        return generate_config

    @classmethod
    def match(
        cls, llm_family: "LLMFamilyV1", llm_spec: "LLMSpecV1", quantization: str
    ) -> bool:
        if not cls._has_cuda_device():
            return False
        if not cls._is_linux():
            return False
        if llm_spec.model_format not in ["pytorch", "gptq", "awq", "fp8"]:
            return False
        if llm_spec.model_format == "pytorch":
            if quantization != "none" and not (quantization is None):
                return False
        if llm_spec.model_format in ["gptq", "awq"]:
            # Currently, only 4-bit weight quantization is supported for GPTQ, but got 8 bits.
            if "4" not in quantization:
                return False
        if isinstance(llm_family, CustomLLMFamilyV1):
            if llm_family.model_family not in KSANA_SUPPORTED_MODELS:
                return False
        else:
            if llm_family.model_name not in KSANA_SUPPORTED_MODELS:
                return False
        if "generate" not in llm_family.model_ability:
            return False
        return KSANA_INSTALLED

    @staticmethod
    def _convert_state_to_completion_chunk(
        request_id: str, model: str, output_text: str
    ) -> CompletionChunk:
        choices: List[CompletionChoice] = [
            CompletionChoice(
                text=output_text,
                index=0,
                logprobs=None,
                finish_reason=None,
            )
        ]
        chunk = CompletionChunk(
            id=request_id,
            object="text_completion",
            created=int(time.time()),
            model=model,
            choices=choices,
        )
        return chunk

    @staticmethod
    def _convert_state_to_completion(
        request_id: str, model: str, output_text: str, meta_info: Dict
    ) -> Completion:
        choices = [
            CompletionChoice(
                text=output_text,
                index=0,
                logprobs=None,
                finish_reason=None,
            )
        ]

        usage = CompletionUsage(
            prompt_tokens=meta_info["prompt_tokens"],
            completion_tokens=meta_info["completion_tokens"],
            total_tokens=meta_info["prompt_tokens"] + meta_info["completion_tokens"],
        )
        return Completion(
            id=request_id,
            object="text_completion",
            created=int(time.time()),
            model=model,
            choices=choices,
            usage=usage,
        )

    @classmethod
    def _filter_sampling_params(cls, sampling_params: dict):
        if not sampling_params.get("lora_name"):
            sampling_params.pop("lora_name", None)
        return sampling_params

    async def _stream_generate(self, prompt: str, **sampling_params):
        import aiohttp

        sampling_params = self._filter_sampling_params(sampling_params)
        json_data = {
            "text": prompt,
            "sampling_params": sampling_params,
            "stream": True,
        }
        pos = 0

        timeout = aiohttp.ClientTimeout(total=3 * 3600)
        async with aiohttp.ClientSession(timeout=timeout, trust_env=True) as session:
            async with session.post(
                self._engine.generate_url, json=json_data  # type: ignore
            ) as response:
                async for chunk, _ in response.content.iter_chunks():
                    chunk = chunk.decode("utf-8")
                    if chunk and chunk.startswith("data:"):
                        stop = "data: [DONE]\n\n"
                        need_stop = False
                        if chunk.endswith(stop):
                            chunk = chunk[: -len(stop)]
                            need_stop = True
                        if chunk:
                            data = json.loads(chunk[5:].strip("\n"))
                            cur = data["text"][pos:]
                            if cur:
                                yield data["meta_info"], cur
                            pos += len(cur)
                            if need_stop:
                                break

    async def _non_stream_generate(self, prompt: str, **sampling_params) -> dict:
        import aiohttp

        sampling_params = self._filter_sampling_params(sampling_params)
        json_data = {
            "text": prompt,
            "sampling_params": sampling_params,
        }
        async with aiohttp.ClientSession(trust_env=True) as session:
            async with session.post(
                self._engine.generate_url, json=json_data  # type: ignore
            ) as response:
                return await response.json()

    async def async_generate(
        self,
        prompt: str,
        generate_config: Optional[KSANAGenerateConfig] = None,
        request_id: Optional[str] = None,
    ) -> Union[Completion, AsyncGenerator[CompletionChunk, None]]:
        sanitized_generate_config = self._sanitize_generate_config(generate_config)
        logger.debug(
            "Enter generate, prompt: %s, generate config: %s", prompt, generate_config
        )
        stream = sanitized_generate_config.pop("stream")
        stream_options = sanitized_generate_config.pop("stream_options")

        include_usage = (
            stream_options.pop("include_usage")
            if isinstance(stream_options, dict)
            else False
        )
        if not request_id:
            request_id = str(uuid.uuid1())
        if not stream:
            state = await self._non_stream_generate(prompt, **sanitized_generate_config)
            return self._convert_state_to_completion(
                request_id,
                model=self.model_uid,
                output_text=state["text"],
                meta_info=state["meta_info"],
            )
        else:

            async def stream_results() -> AsyncGenerator[CompletionChunk, None]:
                prompt_tokens, completion_tokens, total_tokens = 0, 0, 0
                finish_reason = None
                async for meta_info, out in self._stream_generate(
                    prompt, **sanitized_generate_config
                ):
                    chunk = self._convert_state_to_completion_chunk(
                        request_id, self.model_uid, output_text=out
                    )
                    finish_reason = meta_info["finish_reason"]
                    prompt_tokens = meta_info["prompt_tokens"]
                    completion_tokens = meta_info["completion_tokens"]
                    total_tokens = prompt_tokens + completion_tokens
                    chunk["usage"] = CompletionUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    )
                    yield chunk

                finish_reason = (
                    "stop"
                    if finish_reason is None
                    or (
                        isinstance(finish_reason, str)
                        and finish_reason.lower() == "none"
                    )
                    else finish_reason
                )
                yield generate_completion_chunk(
                    "",
                    finish_reason=finish_reason,
                    chunk_id=request_id,
                    model_uid=self.model_uid,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                )

                if include_usage:
                    chunk = CompletionChunk(
                        id=request_id,
                        object="text_completion",
                        created=int(time.time()),
                        model=self.model_uid,
                        choices=[],
                    )
                    chunk["usage"] = CompletionUsage(
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                    )
                    yield chunk

            return stream_results()


class KSANAChatModel(KSANAModel, ChatModelMixin):
    @classmethod
    def match(
        cls, llm_family: "LLMFamilyV1", llm_spec: "LLMSpecV1", quantization: str
    ) -> bool:
        if llm_spec.model_format not in ["pytorch", "gptq", "awq", "fp8"]:
            return False
        if llm_spec.model_format == "pytorch":
            if quantization != "none" and not (quantization is None):
                return False
        if llm_spec.model_format in ["gptq", "awq"]:
            # Currently, only 4-bit weight quantization is supported for GPTQ, but got 8 bits.
            if "4" not in quantization:
                return False
        if isinstance(llm_family, CustomLLMFamilyV1):
            if llm_family.model_family not in KSANA_SUPPORTED_CHAT_MODELS:
                return False
        else:
            if llm_family.model_name not in KSANA_SUPPORTED_CHAT_MODELS:
                return False
        if "chat" not in llm_family.model_ability:
            return False
        return KSANA_INSTALLED

    def _sanitize_chat_config(
        self,
        generate_config: Optional[Dict] = None,
    ) -> Dict:
        if not generate_config:
            generate_config = {}
        if self.model_family.stop:
            if (not generate_config.get("stop")) and self.model_family.stop:
                generate_config["stop"] = self.model_family.stop.copy()
        return generate_config

    async def async_chat(
        self,
        messages: List[Dict],
        generate_config: Optional[Dict] = None,
        request_id: Optional[str] = None,
    ) -> Union[ChatCompletion, AsyncGenerator[ChatCompletionChunk, None]]:
        assert self.model_family.chat_template is not None
        full_prompt = self.get_full_context(messages, self.model_family.chat_template)

        generate_config = self._sanitize_chat_config(generate_config)
        stream = generate_config.get("stream", None)
        if stream:
            agen = await self.async_generate(full_prompt, generate_config)  # type: ignore
            assert isinstance(agen, AsyncGenerator)
            return self._async_to_chat_completion_chunks(agen)
        else:
            c = await self.async_generate(full_prompt, generate_config)  # type: ignore
            assert not isinstance(c, AsyncGenerator)
            return self._to_chat_completion(c)