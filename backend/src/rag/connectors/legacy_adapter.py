"""Presents the connectors brought over from the previous code base (`rag.connectors.legacy`) as connectors of this system.

  old contract                                      this system
  scan(cursor) -> ScanBatch(changes, cursor, done)  changes(cursor) -> ChangeSet(items, cursor, done, complete_listing)
  Change("upsert", id, SourceFile)                  RemoteItem(...)
  Change("delete", id)                              RemoteItem(..., deleted=True)
  complete_scope == "" on the last batch            complete_listing=True (unseen items are then deleted)
  open(SourceFile) -> file object                   download(RemoteItem) -> bytes
  refresh_acl(SourceFile) -> principals             permissions(RemoteItem) -> principals | None

Deletion stays as safe as it was: the old connectors set `complete_scope=""` only after a full, uninterrupted listing (a
resumed, incremental or truncated scan never does), and a listing that is authoritative for a sub-folder only
(`complete_scope="some/prefix"`) is not treated as complete here, because this system can only delete across a whole source.

Principals: "*" becomes "everyone". A source whose config sets neither `acl_rules` nor `default_acl` and whose connector
reports no principals of its own falls back to the source's default ACL (as the native connectors do), instead of the old
"nobody can read it".

The old connectors are synchronous; every call runs in a worker thread so the event loop is never blocked.
"""

from __future__ import annotations

import asyncio
import importlib
from collections.abc import AsyncIterator
from typing import Any, ClassVar

from rag.connectors.base import ChangeSet, RemoteItem, connector_kind
from rag.connectors.legacy import LEGACY_CONNECTORS
from rag.errors import PermanentError


def _principals(values: list[str] | None) -> list[str]:
    return sorted({"everyone" if v == "*" else v for v in (values or []) if v})


class LegacyConnector:
    kind: ClassVar[str] = ""
    target: ClassVar[str] = ""

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        self.config = dict(config or {})
        self.settings = settings
        module, _, cls = self.target.partition(":")
        try:
            self._cls = getattr(importlib.import_module(f"rag.connectors.legacy.{module}"), cls)
        except ImportError as exc:  # an optional package (boto3, paramiko ...) that is not installed
            raise PermanentError(f"source kind {self.kind!r} needs a package that is not installed: {exc}") from exc
        self._impl: Any = None
        self._connector()  # built now so a source with an incomplete config is refused when it is created, not at sync

    def _connector(self) -> Any:
        if self._impl is None:
            try:
                self._impl = self._cls(self.config)
            except KeyError as exc:
                raise PermanentError(f"{self.kind} source config is missing {exc}") from exc
        return self._impl

    def _explicit_acl(self) -> bool:
        return bool(self.config.get("acl_rules") or self.config.get("default_acl"))

    # ---- old <-> new values ---------------------------------------------------------------------------------------------
    def _item(self, change: Any) -> RemoteItem:
        sf = change.file
        if change.kind == "delete" or sf is None:
            return RemoteItem(external_id=change.external_id, path=change.external_id,
                              name=change.external_id.rsplit("/", 1)[-1], deleted=True)
        acl = _principals(sf.acl)
        return RemoteItem(external_id=sf.external_id, path=sf.path, name=sf.name, etag=sf.version, size=sf.size,
                          modified_at=sf.modified_at, mime=sf.mime or None,
                          acl=acl if (acl or self._explicit_acl()) else None)

    @staticmethod
    def _source_file(item: RemoteItem) -> Any:
        from rag.connectors.legacy.base import SourceFile

        return SourceFile(external_id=item.external_id, path=item.path, name=item.name, size=int(item.size or 0),
                          modified_at=item.modified_at, version=item.etag or "", acl=[], mime=item.mime or "")

    # ---- contract --------------------------------------------------------------------------------------------------------
    async def test(self) -> dict[str, Any]:
        connector = self._connector()
        if hasattr(connector, "test"):
            result = await asyncio.to_thread(connector.test)
            return result if isinstance(result, dict) else {"ok": True, "detail": str(result)}
        batches = iter(connector.scan({}))
        first = await asyncio.to_thread(next, batches, None)
        count = len(first.changes) if first is not None else 0
        return {"ok": True, "detail": f"{self.kind}: reachable, first page lists {count} item(s)"}

    async def changes(self, cursor: dict[str, Any]) -> AsyncIterator[ChangeSet]:
        batches = iter(self._connector().scan(dict(cursor or {})))
        while True:
            batch = await asyncio.to_thread(next, batches, None)
            if batch is None:
                return
            yield ChangeSet(items=[self._item(c) for c in batch.changes], cursor=dict(batch.cursor or {}),
                            complete_listing=batch.complete_scope == "", done=bool(batch.done))
            if batch.done:
                return

    async def download(self, item: RemoteItem) -> bytes:
        def read() -> bytes:
            handle = self._connector().open(self._source_file(item))
            try:
                return handle.read()
            finally:
                handle.close()

        return await asyncio.to_thread(read)

    async def permissions(self, item: RemoteItem) -> list[str] | None:
        values = await asyncio.to_thread(self._connector().refresh_acl, self._source_file(item))
        acl = _principals(values)
        return acl if (acl or self._explicit_acl()) else None

    async def aclose(self) -> None:
        close = getattr(self._impl, "close", None)
        if callable(close):
            await asyncio.to_thread(close)


def _register() -> None:
    for kind, target in LEGACY_CONNECTORS.items():
        cls = type(f"Legacy_{kind}", (LegacyConnector,), {"kind": kind, "target": target})
        connector_kind(kind)(cls)


_register()
