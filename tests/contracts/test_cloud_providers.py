"""Cloud providers against recorded API shapes (respx mocks): request format, headers, privacy flags, streaming,
tool calls, usage/cost, error mapping. No keys or network needed."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from rag.config.roles import parse_role
from rag.config.settings import Settings
from rag.errors import PermanentError, RateLimited, TransientError
from rag.plugins.capabilities import ChatMessage, ToolSpec
from rag.plugins.registry import load_plugins, registry

load_plugins()


def _settings(**overrides: object) -> Settings:
    base = {"openrouter_api_key": "sk-or-test", "anthropic_api_key": "sk-ant-test", "openai_api_key": "sk-test",
            "cohere_api_key": "co-test", "azure_di_endpoint": "https://di.example.com", "azure_di_key": "di-key",
            "mistral_api_key": "mi-test", "llama_cloud_api_key": "llx-test", "llm_max_retries": 0}
    base.update(overrides)
    return Settings(_env_file=None, **base)  # type: ignore[arg-type]


def _make(role: str, line: str, settings: Settings | None = None):  # noqa: ANN202
    return registry.create(parse_role(role, line), settings or _settings(), metered=False)


def _sse(*events: dict) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode() + b"data: [DONE]\n\n"


@respx.mock
async def test_openrouter_sends_zdr_and_reads_cost() -> None:
    route = respx.post("https://openrouter.ai/api/v1/chat/completions").mock(return_value=httpx.Response(200, json={
        "model": "anthropic/claude-haiku-5.5", "choices": [{"message": {"content": "ready"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 2, "cost": 0.00042}}))
    chat = _make("LLM_ANSWER", "openrouter/anthropic/claude-haiku-5.5")
    result = await chat.complete([ChatMessage("user", "hi")])
    body = json.loads(route.calls[0].request.content)
    headers = route.calls[0].request.headers
    assert body["model"] == "anthropic/claude-haiku-5.5"
    assert body["provider"] == {"zdr": True, "data_collection": "deny"}
    assert body["usage"] == {"include": True}
    assert headers["authorization"] == "Bearer sk-or-test" and headers["x-title"]
    assert result.content == "ready" and result.usage.cost_usd == 0.00042 and result.usage.input_tokens == 12
    await chat.aclose()


@respx.mock
async def test_openrouter_streaming_and_tool_calls() -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(side_effect=[
        httpx.Response(200, content=_sse(
            {"choices": [{"delta": {"content": "Grade "}}]},
            {"choices": [{"delta": {"content": "C40 [1]."}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 5, "cost": 0.001}}),
            headers={"content-type": "text/event-stream"}),
        httpx.Response(200, json={"choices": [{"message": {"content": None, "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": "search", "arguments": '{"query": "C40"}'}}]},
            "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 10, "completion_tokens": 4}}),
    ])
    chat = _make("LLM_ANSWER", "openrouter/anthropic/claude-haiku-5.5")
    chunks = [c async for c in chat.stream([ChatMessage("user", "q")])]
    assert "".join(c.delta for c in chunks) == "Grade C40 [1]."
    assert chunks[-1].result.usage.output_tokens == 5 and chunks[-1].result.usage.cost_usd == 0.001
    tool = ToolSpec("search", "search", {"type": "object", "properties": {"query": {"type": "string"}}})
    result = await chat.complete([ChatMessage("user", "q")], tools=[tool])
    assert result.tool_calls[0].name == "search" and result.tool_calls[0].arguments == {"query": "C40"}
    await chat.aclose()


@respx.mock
async def test_anthropic_messages_api_shape_and_tools() -> None:
    route = respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(200, json={
        "model": "claude-haiku-5-5", "stop_reason": "tool_use",
        "content": [{"type": "text", "text": "Looking."},
                    {"type": "tool_use", "id": "tu_1", "name": "read_pages", "input": {"document": "A-101",
                                                                                     "pages": [2]}}],
        "usage": {"input_tokens": 30, "output_tokens": 9}}))
    chat = _make("LLM_ANSWER", "anthropic/claude-haiku-5-5")
    tool = ToolSpec("read_pages", "read", {"type": "object", "properties": {}})
    result = await chat.complete([ChatMessage("system", "rules"), ChatMessage("user", "q")], tools=[tool])
    body = json.loads(route.calls[0].request.content)
    assert route.calls[0].request.headers["anthropic-version"] == "2023-06-01"
    assert body["system"] == "rules" and body["messages"] == [{"role": "user", "content": [{"type": "text",
                                                                                            "text": "q"}]}]
    assert body["tools"][0]["input_schema"] == {"type": "object", "properties": {}}
    assert result.tool_calls[0].arguments == {"document": "A-101", "pages": [2]}
    await chat.aclose()


@respx.mock
async def test_anthropic_streaming_events() -> None:
    events = [
        {"type": "message_start", "message": {"model": "claude-haiku-5-5", "usage": {"input_tokens": 50}}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Cover is "}},
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "50 mm [2]."}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]
    body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
    respx.post("https://api.anthropic.com/v1/messages").mock(return_value=httpx.Response(
        200, content=body, headers={"content-type": "text/event-stream"}))
    chat = _make("LLM_ANSWER", "anthropic/claude-haiku-5-5")
    chunks = [c async for c in chat.stream([ChatMessage("user", "q")])]
    final = chunks[-1].result
    assert final.content == "Cover is 50 mm [2]." and final.usage.input_tokens == 50 and final.usage.output_tokens == 7
    await chat.aclose()


@pytest.mark.parametrize(("status", "error"), [(429, RateLimited), (503, TransientError), (401, PermanentError)])
@respx.mock
async def test_http_errors_map_to_retry_classes(status: int, error: type[Exception]) -> None:
    respx.post("https://openrouter.ai/api/v1/chat/completions").mock(return_value=httpx.Response(
        status, json={"error": {"message": "nope"}}, headers={"retry-after": "7"}))
    chat = _make("LLM_ANSWER", "openrouter/anthropic/claude-haiku-5.5")
    with pytest.raises(error) as exc_info:
        await chat.complete([ChatMessage("user", "q")])
    if status == 429:
        assert exc_info.value.retry_after == 7
    assert "sk-or-test" not in str(exc_info.value)
    await chat.aclose()


@respx.mock
async def test_openai_embeddings_with_dimensions() -> None:
    route = respx.post("https://api.openai.com/v1/embeddings").mock(return_value=httpx.Response(200, json={
        "data": [{"index": 1, "embedding": [0.0] * 1024}, {"index": 0, "embedding": [1.0] + [0.0] * 1023}]}))
    embedder = _make("EMBEDDER", "openai/text-embedding-3-large@1024")
    vectors = await embedder.embed_documents(["a", "b"])
    assert json.loads(route.calls[0].request.content)["dimensions"] == 1024
    assert vectors[0][0] == 1.0 and embedder.slug == "openai-text-embedding-3-large-1024"
    await embedder.aclose()


@respx.mock
async def test_cohere_rerank_orders_scores_by_index() -> None:
    respx.post("https://api.cohere.com/v2/rerank").mock(return_value=httpx.Response(200, json={
        "results": [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.2}]}))
    reranker = _make("RERANKER", "cohere/rerank-v3.5")
    assert await reranker.rerank("q", ["a", "b"]) == [0.2, 0.9]
    assert reranker.languages is None  # multilingual: Arabic questions are reranked too
    await reranker.aclose()


@respx.mock
async def test_azure_document_intelligence_ocr_polls_the_operation() -> None:
    respx.post(url__startswith="https://di.example.com/documentintelligence/documentModels/prebuilt-read:analyze"
               ).mock(return_value=httpx.Response(202, headers={"operation-location": "https://di.example.com/op/1"}))
    respx.get("https://di.example.com/op/1").mock(side_effect=[
        httpx.Response(200, json={"status": "running"}),
        httpx.Response(200, json={"status": "succeeded", "analyzeResult": {"content": "x", "pages": [{
            "width": 100, "height": 200, "lines": [{"content": "تعليمات الموقع", "polygon": [10, 20, 90, 20, 90, 40,
                                                                                            10, 40]}],
            "words": [{"confidence": 0.98}]}]}}),
    ])
    ocr = _make("OCR", "azure_di/prebuilt-read")
    import rag.providers.ocr.cloud as cloud

    original = cloud.asyncio.sleep

    async def no_sleep(_: float) -> None:
        await original(0)

    cloud.asyncio.sleep = no_sleep  # type: ignore[assignment]
    try:
        result = await ocr.recognize(b"\x89PNG fake")
    finally:
        cloud.asyncio.sleep = original  # type: ignore[assignment]
    assert result.text == "تعليمات الموقع" and result.lines[0].box == (0.1, 0.1, 0.9, 0.2)
    await ocr.aclose()


@respx.mock
async def test_mistral_pdf_parser_pages() -> None:
    respx.post("https://api.mistral.ai/v1/ocr").mock(return_value=httpx.Response(200, json={
        "pages": [{"index": 0, "markdown": "# SECTION 03 30 00"}, {"index": 1, "markdown": "Columns C40"}]}))
    parser = _make("PARSER", "mistral/mistral-ocr-latest")
    doc = await parser.parse_pdf(b"%PDF-1.7 fake", "spec.pdf")
    assert [p.number for p in doc.pages] == [1, 2] and doc.pages[1].text == "Columns C40"
    await parser.aclose()


def test_missing_keys_are_reported_before_any_call() -> None:
    from rag.plugins.registry import PluginError

    with pytest.raises(PluginError, match="OPENROUTER_API_KEY"):
        registry.create(parse_role("LLM_ANSWER", "openrouter/anthropic/claude-haiku-5.5"),
                        _settings(openrouter_api_key=None))
