"""ASGI entry point: `uvicorn rag.api.main:app`."""

from rag.api.app import create_app

app = create_app()
