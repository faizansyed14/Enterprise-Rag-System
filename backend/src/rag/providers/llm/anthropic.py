"""Claude through Anthropic's Messages API directly (`anthropic/claude-haiku-5-5`)."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import httpx

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings
from rag.errors import TransientError
from rag.plugins.capabilities import ChatMessage, ChatResult, StreamChunk, ToolCall, ToolSpec, Usage
from rag.plugins.registry import registry
from rag.providers.http import check_response, make_client, with_retries
from rag.providers.llm.pricing import cost_for

API_VERSION = "2023-06-01"


def to_anthropic(messages: Sequence[ChatMessage]) -> tuple[str, list[dict[str, Any]]]:
    system_parts: list[str] = []
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": blocks})

    for m in messages:
        if m.role == "system":
            system_parts.append(m.content if isinstance(m.content, str) else "")
            continue
        if m.role == "tool":
            push("user", [{"type": "tool_result", "tool_use_id": m.tool_call_id or "",
                           "content": m.content if isinstance(m.content, str) else json.dumps(m.content)}])
            continue
        blocks: list[dict[str, Any]] = []
        if isinstance(m.content, list):
            for part in m.content:
                if part.get("type") == "image":
                    blocks.append({"type": "image", "source": {"type": "base64", "media_type": part["media_type"],
                                                               "data": part["data"]}})
                elif part.get("text"):
                    blocks.append({"type": "text", "text": part["text"]})
        elif m.content:
            blocks.append({"type": "text", "text": m.content})
        blocks.extend({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments}
                      for call in m.tool_calls)
        if blocks:
            push("assistant" if m.role == "assistant" else "user", blocks)
    return "\n\n".join(p for p in system_parts if p), out


class AnthropicChat:
    supports_tools = True
    supports_vision = True

    def __init__(self, spec: RoleSpec, settings: Settings) -> None:
        self.spec = spec
        self.settings = settings
        self._client = make_client(
            settings.anthropic_base_url.rstrip("/"),
            timeout=settings.llm_timeout_s,
            headers={"x-api-key": settings.secret("anthropic_api_key") or "", "anthropic-version": API_VERSION},
        )

    def _body(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] | None, max_tokens: int | None,
              temperature: float | None, stream: bool) -> dict[str, Any]:
        system, converted = to_anthropic(messages)
        body: dict[str, Any] = {
            "model": self.spec.model,
            "max_tokens": max_tokens or self.settings.llm_answer_max_tokens,
            "messages": converted,
            "temperature": self.settings.llm_temperature if temperature is None else temperature,
        }
        if system:
            body["system"] = system
        if tools:
            body["tools"] = [t.anthropic_schema() for t in tools]
        if stream:
            body["stream"] = True
        return body

    def _usage(self, data: dict[str, Any]) -> Usage:
        usage = Usage(
            input_tokens=int(data.get("input_tokens") or 0) + int(data.get("cache_read_input_tokens") or 0)
            + int(data.get("cache_creation_input_tokens") or 0),
            output_tokens=int(data.get("output_tokens") or 0),
            cached_input_tokens=int(data.get("cache_read_input_tokens") or 0),
        )
        usage.cost_usd = cost_for(f"anthropic/{self.spec.model}", usage, self.settings.model_prices_file)
        return usage

    async def complete(self, messages: Sequence[ChatMessage], *, tools: Sequence[ToolSpec] | None = None,
                       max_tokens: int | None = None, temperature: float | None = None, json_mode: bool = False,
                       timeout: float | None = None) -> ChatResult:
        body = self._body(messages, tools=tools, max_tokens=max_tokens, temperature=temperature, stream=False)

        async def call() -> dict[str, Any]:
            response = await self._client.post("/v1/messages", json=body,
                                               timeout=timeout or self.settings.llm_timeout_s)
            return check_response(response, "anthropic").json()

        data = await with_retries(call, provider="anthropic", attempts=1 + self.settings.llm_max_retries)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {}))
        return ChatResult(content="".join(text_parts), tool_calls=calls, usage=self._usage(data.get("usage") or {}),
                          model=data.get("model") or self.spec.model, finish_reason=data.get("stop_reason") or "")

    async def stream(self, messages: Sequence[ChatMessage], *, max_tokens: int | None = None,
                     temperature: float | None = None, timeout: float | None = None) -> AsyncIterator[StreamChunk]:
        body = self._body(messages, tools=None, max_tokens=max_tokens, temperature=temperature, stream=True)
        request = self._client.build_request("POST", "/v1/messages", json=body,
                                             timeout=timeout or self.settings.llm_timeout_s)
        try:
            response = await self._client.send(request, stream=True)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"anthropic: {type(exc).__name__}: {exc}") from exc
        content: list[str] = []
        usage_data: dict[str, Any] = {}
        model = self.spec.model
        finish = ""
        try:
            if response.status_code >= 400:
                await response.aread()
                check_response(response, "anthropic")
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    event = json.loads(line[5:].strip())
                except ValueError:
                    continue
                kind = event.get("type")
                if kind == "message_start":
                    message = event.get("message") or {}
                    model = message.get("model") or model
                    usage_data.update(message.get("usage") or {})
                elif kind == "content_block_delta":
                    delta = event.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        content.append(delta["text"])
                        yield StreamChunk(delta=delta["text"])
                elif kind == "message_delta":
                    usage_data.update({k: v for k, v in (event.get("usage") or {}).items() if v is not None})
                    finish = (event.get("delta") or {}).get("stop_reason") or finish
                elif kind == "error":
                    raise TransientError(f"anthropic stream error: {str(event.get('error'))[:200]}")
                elif kind == "message_stop":
                    break
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise TransientError(f"anthropic stream interrupted: {type(exc).__name__}") from exc
        finally:
            await response.aclose()
        yield StreamChunk(result=ChatResult(content="".join(content), usage=self._usage(usage_data), model=model,
                                            finish_reason=finish))

    async def aclose(self) -> None:
        await self._client.aclose()


for _cap in (Capability.CHAT, Capability.VISION):
    registry.register(_cap, "anthropic", requires=("anthropic_api_key",),
                      description="Anthropic Messages API (Claude)")(AnthropicChat)
