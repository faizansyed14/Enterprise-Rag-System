"""Chat models behind OpenAI-style /chat/completions APIs: OpenRouter, OpenAI, Azure OpenAI and any compatible server
(vLLM, LM Studio, Ollama's OpenAI endpoint, a LiteLLM proxy) via `openai_compat/<model>` + OPENAI_COMPAT_BASE_URL.

OpenRouter specifics: zero-data-retention routing when OPENROUTER_ZDR=true (provider.zdr and data_collection=deny,
so prompts only go to endpoints that keep nothing), and `usage.include` so every call reports its real cost.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import PermanentError, TransientError
from rag.plugins.capabilities import ChatMessage, ChatResult, StreamChunk, ToolCall, ToolSpec, Usage
from rag.plugins.registry import registry
from rag.providers.http import check_response, make_client, with_retries
from rag.providers.llm.pricing import cost_for

log = logging.getLogger(__name__)


def to_openai_messages(messages: Sequence[ChatMessage]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": _text(m.content)})
            continue
        entry: dict[str, Any] = {"role": m.role}
        if isinstance(m.content, list):
            parts = []
            for part in m.content:
                if part.get("type") == "image":
                    parts.append({"type": "image_url",
                                  "image_url": {"url": f"data:{part['media_type']};base64,{part['data']}"}})
                else:
                    parts.append({"type": "text", "text": part.get("text", "")})
            entry["content"] = parts
        else:
            entry["content"] = m.content
        if m.tool_calls:
            entry["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                for c in m.tool_calls
            ]
            if not m.content:
                entry["content"] = None
        out.append(entry)
    return out


def _text(content: str | list[dict[str, Any]]) -> str:
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content if p.get("type") == "text")


def _parse_arguments(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return {"_raw": raw}
    return value if isinstance(value, dict) else {"value": value}


class OpenAICompatChat:
    supports_tools = True
    supports_vision = True

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        self.provider = spec.provider
        headers: dict[str, str] = {}
        if spec.provider == "openrouter":
            base = settings.openrouter_base_url
            headers["Authorization"] = f"Bearer {settings.secret('openrouter_api_key')}"
            if settings.openrouter_site_url:
                headers["HTTP-Referer"] = settings.openrouter_site_url
            headers["X-Title"] = settings.openrouter_app_name
            self._path = "/chat/completions"
        elif spec.provider == "openai":
            base = settings.openai_base_url
            headers["Authorization"] = f"Bearer {settings.secret('openai_api_key')}"
            self._path = "/chat/completions"
        elif spec.provider == "azure_openai":
            base = settings.azure_openai_endpoint.rstrip("/")
            headers["api-key"] = settings.secret("azure_openai_api_key") or ""
            self._path = (f"/openai/deployments/{spec.model}/chat/completions"
                          f"?api-version={settings.azure_openai_api_version}")
        else:  # openai_compat
            base = settings.openai_compat_base_url
            key = settings.secret("openai_compat_api_key")
            if key:
                headers["Authorization"] = f"Bearer {key}"
            self._path = "/chat/completions"
        self._client = make_client(base.rstrip("/"), timeout=settings.llm_timeout_s, headers=headers)

    # ---- request building ----------------------------------------------------------------------------------------
    def _body(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] | None, max_tokens: int | None,
              temperature: float | None, json_mode: bool, stream: bool) -> dict[str, Any]:
        body: dict[str, Any] = {"messages": to_openai_messages(messages)}
        if self.provider != "azure_openai":
            body["model"] = self.spec.model
        limit = max_tokens or self.settings.llm_answer_max_tokens
        if self.provider in {"openai", "azure_openai"}:
            body["max_completion_tokens"] = limit
        else:
            body["max_tokens"] = limit
        temp = self.settings.llm_temperature if temperature is None else temperature
        if self._accepts_temperature():
            body["temperature"] = temp
        if tools:
            body["tools"] = [t.openai_schema() for t in tools]
            body["tool_choice"] = "auto"
        if json_mode and self.provider in {"openai", "azure_openai"}:
            body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        if self.provider == "openrouter":
            body["usage"] = {"include": True}
            if self.settings.openrouter_zdr:
                body["provider"] = {"zdr": True, "data_collection": "deny"}
        return body

    def _accepts_temperature(self) -> bool:
        if self.spec.options.get("temperature") == "default":
            return False
        model = self.spec.model.lower()
        return not (self.provider in {"openai", "azure_openai"} and (model.startswith(("o1", "o3", "o4", "gpt-5"))))

    def _usage(self, data: dict[str, Any] | None) -> Usage:
        data = data or {}
        details = data.get("prompt_tokens_details") or {}
        usage = Usage(
            input_tokens=int(data.get("prompt_tokens") or 0),
            output_tokens=int(data.get("completion_tokens") or 0),
            cached_input_tokens=int(details.get("cached_tokens") or 0),
        )
        if data.get("cost") is not None:
            usage.cost_usd = float(data["cost"])
        else:
            usage.cost_usd = cost_for(f"{self.provider}/{self.spec.model}", usage, self.settings.model_prices_file)
        return usage

    # ---- calls -----------------------------------------------------------------------------------------------------
    async def complete(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] | None = None,
                       max_tokens: int | None = None, temperature: float | None = None, json_mode: bool = False,
                       timeout: float | None = None) -> ChatResult:
        body = self._body(messages, tools=tools, max_tokens=max_tokens, temperature=temperature,
                          json_mode=json_mode, stream=False)

        async def call() -> dict[str, Any]:
            response = await self._client.post(self._path, json=body, timeout=timeout or self.settings.llm_timeout_s)
            return check_response(response, self.provider).json()

        data = await with_retries(call, provider=self.provider, attempts=1 + self.settings.llm_max_retries)
        if "choices" not in data or not data["choices"]:
            raise TransientError(f"{self.provider} returned no choices: {str(data)[:200]}")
        choice = data["choices"][0]
        message = choice.get("message") or {}
        calls = [
            ToolCall(id=c.get("id") or f"call_{i}", name=c["function"]["name"],
                     arguments=_parse_arguments(c["function"].get("arguments")))
            for i, c in enumerate(message.get("tool_calls") or [])
        ]
        return ChatResult(content=message.get("content") or "", tool_calls=calls, usage=self._usage(data.get("usage")),
                          model=data.get("model") or self.spec.model, finish_reason=choice.get("finish_reason") or "")

    async def stream(self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None,
                     temperature: float | None = None, timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        body = self._body(messages, tools=None, max_tokens=max_tokens, temperature=temperature, json_mode=False,
                          stream=True)
        content: list[str] = []
        usage = Usage()
        model = self.spec.model
        finish = ""
        request = self._client.build_request("POST", self._path, json=body,
                                             timeout=timeout or self.settings.llm_timeout_s)
        try:
            response = await self._client.send(request, stream=True)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"{self.provider}: {type(exc).__name__}: {exc}") from exc
        try:
            if response.status_code >= 400:
                await response.aread()
                check_response(response, self.provider)
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    data = json.loads(payload)
                except ValueError:
                    continue
                if data.get("error"):
                    raise TransientError(f"{self.provider} stream error: {str(data['error'])[:200]}")
                model = data.get("model") or model
                if data.get("usage"):
                    usage = self._usage(data["usage"])
                for choice in data.get("choices") or []:
                    delta = (choice.get("delta") or {}).get("content")
                    if delta:
                        content.append(delta)
                        yield StreamChunk(delta=delta)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"{self.provider} stream interrupted: {type(exc).__name__}") from exc
        finally:
            await response.aclose()
        yield StreamChunk(result=ChatResult(content="".join(content), usage=usage, model=model, finish_reason=finish))

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAICompatEmbedder:
    """Embeddings over /embeddings (OpenAI, Azure OpenAI, OpenRouter, compatible servers). `@dims` asks the API for
    shortened vectors where the model supports it (text-embedding-3-*)."""

    multilingual = True

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        chat = OpenAICompatChat(spec, settings)  # reuse auth/base URL logic
        self._client = chat._client
        self._path = chat._path.replace("chat/completions", "embeddings")
        self.dims = spec.dims or int(spec.options.get("dims", 0)) or _KNOWN_DIMS.get(spec.model, 1536)
        self.max_tokens = int(spec.options.get("max_tokens", 8000))
        self.batch = max(1, min(settings.embedder_batch * 4, 256))

    @property
    def slug(self) -> str:
        return f"{self.spec.provider}-{self.spec.model}-{self.dims}".replace("/", "-").lower()

    async def _embed(self, texts: Sequence[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            chunk = [t if t.strip() else " " for t in texts[i: i + self.batch]]
            body: dict[str, Any] = {"input": chunk}
            if self.spec.provider != "azure_openai":
                body["model"] = self.spec.model
            if self.spec.dims:
                body["dimensions"] = self.spec.dims

            async def call(body: dict[str, Any] = body) -> dict[str, Any]:
                response = await self._client.post(self._path, json=body)
                return check_response(response, self.spec.provider).json()

            data = await with_retries(call, provider=self.spec.provider, attempts=1 + self.settings.llm_max_retries)
            rows = sorted(data["data"], key=lambda r: r["index"])
            vectors = [r["embedding"] for r in rows]
            if vectors and len(vectors[0]) != self.dims:
                raise PermanentError(f"{self.spec.label} returned {len(vectors[0])} dims, expected {self.dims}")
            out.extend(vectors)
        return out

    async def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return await self._embed(texts)

    async def embed_query(self, text: str) -> list[float]:
        return (await self._embed([text]))[0]

    async def aclose(self) -> None:
        await self._client.aclose()


_KNOWN_DIMS = {"text-embedding-3-large": 3072, "text-embedding-3-small": 1536, "text-embedding-ada-002": 1536}

_KEYS = {"openrouter": ("openrouter_api_key",), "openai": ("openai_api_key",),
         "azure_openai": ("azure_openai_api_key", "azure_openai_endpoint"), "openai_compat": ("openai_compat_base_url",)}
_DESCRIPTIONS = {
    "openrouter": "OpenRouter (one key for Anthropic, OpenAI, Google and others; zero-data-retention routing)",
    "openai": "OpenAI API",
    "azure_openai": "Azure OpenAI deployment (model = deployment name)",
    "openai_compat": "Any OpenAI-compatible server (vLLM, LM Studio, Ollama, LiteLLM proxy)",
}

for _provider, _keys in _KEYS.items():
    _local = _provider == "openai_compat"
    for _cap in (Capability.CHAT, Capability.VISION):
        registry.register(_cap, _provider, requires=_keys, description=_DESCRIPTIONS[_provider],
                          local=_local)(OpenAICompatChat)
    registry.register(Capability.EMBEDDER, _provider, requires=_keys, description=_DESCRIPTIONS[_provider],
                      local=_local)(OpenAICompatEmbedder)
