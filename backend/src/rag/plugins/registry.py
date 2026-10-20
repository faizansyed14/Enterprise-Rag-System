"""Plugin registry: (capability, provider) -> factory.

Providers register themselves with a decorator when their module is imported:

    @registry.register(Capability.EMBEDDER, "local", requires=("local_models_url",), local=True,
                       description="ONNX model server in this deployment")
    def _make(spec: RoleSpec, settings: Settings) -> Embedder: ...

Third-party packages can add providers without touching this code by exposing an entry point in the
`rag.plugins` group that points at a module; importing that module registers its providers.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from typing import Any

from rag.config.roles import Capability, RoleSpec
from rag.config.settings import Settings

log = logging.getLogger(__name__)

Factory = Callable[[RoleSpec, Settings], Any]


class PluginError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderInfo:
    capability: Capability
    provider: str
    factory: Factory
    requires: tuple[str, ...]
    description: str
    local: bool


class PluginRegistry:
    def __init__(self) -> None:
        self._providers: dict[tuple[Capability, str], ProviderInfo] = {}

    def register(
        self,
        capability: Capability,
        provider: str,
        *,
        requires: tuple[str, ...] = (),
        description: str = "",
        local: bool = False,
    ) -> Callable[[Factory], Factory]:
        def decorator(factory: Factory) -> Factory:
            key = (capability, provider)
            if key in self._providers and self._providers[key].factory is not factory:
                log.warning("provider %s/%s registered twice; keeping the latest", capability, provider)
            self._providers[key] = ProviderInfo(capability, provider, factory, requires, description, local)
            return factory

        return decorator

    def info(self, capability: Capability, provider: str) -> ProviderInfo | None:
        return self._providers.get((capability, provider))

    def providers(self, capability: Capability | None = None) -> list[ProviderInfo]:
        items = [p for p in self._providers.values() if capability is None or p.capability == capability]
        return sorted(items, key=lambda p: (p.capability.value, p.provider))

    def missing_requirements(self, spec: RoleSpec, settings: Settings) -> list[str]:
        info = self.info(spec.capability, spec.provider)
        if info is None:
            return []
        return [name.upper() for name in info.requires if not settings.secret(name)]

    def create(self, spec: RoleSpec, settings: Settings, *, metered: bool = True) -> Any:
        if spec.is_off:
            return None
        info = self.info(spec.capability, spec.provider)
        if info is None:
            known = ", ".join(p.provider for p in self.providers(spec.capability)) or "none"
            raise PluginError(
                f"{spec.role}={spec.label}: no {spec.capability.value} provider named {spec.provider!r}. Known: {known}"
            )
        missing = self.missing_requirements(spec, settings)
        if missing:
            raise PluginError(f"{spec.role}={spec.label} needs {', '.join(missing)} in .env")
        plugin = info.factory(spec, settings)
        if not metered:
            return plugin
        from rag.plugins.metered import wrap

        return wrap(plugin, spec)


registry = PluginRegistry()

_BUILTIN_MODULES = (
    "rag.providers.llm.openai_compat",
    "rag.providers.llm.anthropic",
    "rag.providers.llm.extractive",
    "rag.providers.embedders.local",
    "rag.providers.embedders.hashing",
    "rag.providers.embedders.remote",
    "rag.providers.rerankers.local",
    "rag.providers.rerankers.lexical",
    "rag.providers.rerankers.cohere",
    "rag.providers.groundedness.judges",
    "rag.providers.ocr.rapidocr",
    "rag.providers.ocr.cloud",
    "rag.providers.ocr.vision",
    "rag.providers.parsers.pdf",
    "rag.providers.parsers.cloud",
)

_loaded = False


def load_plugins() -> PluginRegistry:
    """Import built-in providers and any installed `rag.plugins` entry points (idempotent)."""
    global _loaded
    if _loaded:
        return registry
    for module in _BUILTIN_MODULES:
        importlib.import_module(module)
    for ep in entry_points(group="rag.plugins"):
        try:
            importlib.import_module(ep.value)
            log.info("loaded plugin module %s from %s", ep.value, ep.name)
        except Exception:
            log.exception("could not load plugin entry point %s", ep.name)
    _loaded = True
    return registry
