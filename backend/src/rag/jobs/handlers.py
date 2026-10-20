"""Import every module that registers job handlers, so a worker knows all job kinds."""

from __future__ import annotations

import importlib

HANDLER_MODULES = (
    "rag.ingestion.pipeline",
    "rag.security.sync",
    "rag.monitoring.tick",
    "rag.evaluation.runner",
)


def load_handlers() -> None:
    for module in HANDLER_MODULES:
        importlib.import_module(module)
