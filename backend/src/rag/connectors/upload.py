"""`upload` source: files users upload in the UI. The API stores the bytes and registers the item; there is nothing
to list remotely, so syncing is a no-op and downloads read the object store."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, ClassVar

from rag.connectors.base import ChangeSet, RemoteItem, connector_kind
from rag.providers.storage.objects import blob_key, make_object_store


@connector_kind("upload")
class UploadConnector:
    kind: ClassVar[str] = "upload"

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        self.store = make_object_store(settings)

    async def test(self) -> dict[str, Any]:
        return {"ok": True, "detail": "Uploads are stored in the object store"}

    async def changes(self, cursor: dict[str, Any]) -> AsyncIterator[ChangeSet]:
        yield ChangeSet(items=[], cursor=cursor, complete_listing=False, done=True)

    async def download(self, item: RemoteItem) -> bytes:
        # Uploaded items carry the content hash as their etag; the bytes were stored at upload time.
        return await self.store.get(blob_key(item.etag or ""))

    async def permissions(self, item: RemoteItem) -> list[str] | None:
        return None

    async def aclose(self) -> None:
        return None
