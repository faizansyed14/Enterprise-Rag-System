from __future__ import annotations

import pytest

from rag.config.settings import Settings
from rag.errors import ConfigurationError
from rag.plugins.hub import LOCAL_ANSWERER, ModelHub
from rag.providers import openrouter_catalog as catalog

PAYLOAD = {"data": [
    {"id": "openai/gpt-x", "name": "GPT X", "context_length": 128000, "pricing": {"prompt": "0.000002", "completion": "0.00001"},
     "architecture": {"output_modalities": ["text"]}},
    {"id": "meta-llama/llama-free:free", "name": "Llama (free)", "pricing": {"prompt": "0", "completion": "0"}},
    {"id": "some/image-maker", "name": "Image maker", "architecture": {"output_modalities": ["image"]}},
    {"id": "broken", "name": "No vendor"},
]}


def test_the_catalogue_keeps_text_models_with_prices_per_million_tokens() -> None:
    models = catalog.parse(PAYLOAD)
    assert [m["id"] for m in models] == ["meta-llama/llama-free:free", "openai/gpt-x"]
    gpt = models[1]
    assert gpt["prompt_per_m"] == 2.0 and gpt["completion_per_m"] == 10.0 and not gpt["free"]
    assert models[0]["free"]


def _hub(**settings: object) -> ModelHub:
    return ModelHub(Settings(_env_file=None, **settings))  # type: ignore[call-arg]


def test_any_openrouter_model_is_allowed_when_switched_on_and_the_local_answerer_always() -> None:
    hub = _hub(allow_any_openrouter_model=True, openrouter_models="*", model_choices="")
    catalog._cache["models"] = []
    assert hub._any_openrouter("openrouter/openai/gpt-x")
    assert not hub._any_openrouter("anthropic/claude-haiku-5-5")
    assert not hub._any_openrouter("openrouter/nonsense")  # no vendor part
    catalog._cache["models"] = catalog.parse(PAYLOAD)
    assert hub._any_openrouter("openrouter/openai/gpt-x") and not hub._any_openrouter("openrouter/openai/not-listed")
    catalog._cache["models"] = []
    assert LOCAL_ANSWERER == "extractive/default"


def test_switching_it_off_limits_choices_to_the_shortlist() -> None:
    hub = _hub(allow_any_openrouter_model=False, model_choices="openrouter/openai/gpt-x")
    assert not hub._any_openrouter("openrouter/openai/gpt-x")
    with pytest.raises(ConfigurationError):
        hub.chat(choice="openrouter/meta-llama/llama-3")


def test_vision_and_metadata_models_can_be_switched_from_the_app_and_switched_off() -> None:
    hub = _hub()
    hub._overrides = {"VISION": "openrouter/openai/gpt-4o-mini", "LLM_ENRICH": "none", "OCR": "vision/default",
                      "EMBEDDER": "local/bge-m3"}
    assert hub.spec("VISION").label == "openrouter/openai/gpt-4o-mini"
    assert hub.spec("LLM_ENRICH").is_off
    assert hub.spec("OCR").label == "vision/default"  # how scanned pages are read can be changed in the app
    assert hub.spec("EMBEDDER").label == hub.env_spec("EMBEDDER").label  # the embedder changes the index: .env only


def test_the_catalogue_marks_models_that_can_read_images() -> None:
    models = catalog.parse({"data": [
        {"id": "a/vision", "name": "V", "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]}},
        {"id": "a/text", "name": "T", "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]}}]})
    assert {m["id"]: m["vision"] for m in models} == {"a/vision": True, "a/text": False}


def test_the_allow_list_names_models_free_models_or_everything() -> None:
    spec = "anthropic/claude-haiku-5.5,openai/gpt-4o-mini,free"
    assert catalog.allows("anthropic/claude-haiku-5.5", spec) and catalog.allows("openai/gpt-4o-mini", spec)
    assert catalog.allows("cohere/north-mini-code:free", spec)
    assert not catalog.allows("openai/gpt-5", spec) and not catalog.allows("anthropic/claude-opus-5.5", spec)
    assert catalog.allows("openai/gpt-5", "*")
    assert not catalog.allows("cohere/x:free", "anthropic/claude-haiku-5.5")


def test_the_curated_list_and_the_server_agree_on_what_may_be_chosen() -> None:
    models = catalog.parse({"data": [
        {"id": "anthropic/claude-haiku-5.5", "name": "H", "pricing": {"prompt": "0.0000001", "completion": "0.0000005"}},
        {"id": "openai/gpt-4o-mini", "name": "M", "pricing": {"prompt": "0.00000015", "completion": "0.0000006"}},
        {"id": "openai/gpt-5", "name": "G5", "pricing": {"prompt": "0.000005", "completion": "0.00002"}},
        {"id": "meta/llama:free", "name": "L", "pricing": {"prompt": "0", "completion": "0"}}]})
    spec = "anthropic/claude-haiku-5.5,openai/gpt-4o-mini,free"
    assert [m["id"] for m in catalog.curate(models, spec)] == ["anthropic/claude-haiku-5.5", "meta/llama:free", "openai/gpt-4o-mini"]
    hub = _hub(allow_any_openrouter_model=True, openrouter_models=spec)
    catalog._cache["models"] = models
    assert hub._any_openrouter("openrouter/meta/llama:free") and hub._any_openrouter("openrouter/openai/gpt-4o-mini")
    assert not hub._any_openrouter("openrouter/openai/gpt-5")
    catalog._cache["models"] = []


def test_music_safety_and_router_entries_are_not_offered() -> None:
    ids = [m["id"] for m in catalog.parse({"data": [
        {"id": "google/lyria-3-pro-preview", "name": "Lyria"}, {"id": "nvidia/nemotron-3.5-content-safety:free", "name": "Safety"},
        {"id": "openrouter/free", "name": "Free router"}, {"id": "openrouter/auto", "name": "Auto"},
        {"id": "google/gemini-2.5-flash", "name": "Gemini"}]})]
    assert ids == ["google/gemini-2.5-flash"]
