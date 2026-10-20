"""`filesystem` source: a folder on the worker's disk or a mounted share (NFS, SMB mount, Azure Files).

Config: {"root": "/data/projects", "include": ["**/*"], "exclude": ["**/~$*", "**/.git/**"]}
Every sync lists the folder (so deletions are noticed); unchanged files are skipped by size + modification time.
"""

from __future__ import annotations

import asyncio
import fnmatch
import mimetypes
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from rag.connectors.base import ChangeSet, RemoteItem, connector_kind
from rag.errors import PermanentError

DEFAULT_EXCLUDE = ["**/~$*", "**/.~lock*", "**/Thumbs.db", "**/.DS_Store", "**/.git/**", "**/*.tmp", "**/desktop.ini"]
PAGE = 1000


@connector_kind("filesystem")
class FilesystemConnector:
    kind: ClassVar[str] = "filesystem"

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        root = config.get("root")
        if not root:
            raise PermanentError("filesystem source needs a 'root' folder")
        self.root = Path(root).resolve()
        self.include = config.get("include") or ["**/*"]
        self.exclude = DEFAULT_EXCLUDE + list(config.get("exclude") or [])
        self.max_bytes = int(getattr(settings, "max_file_mb", 2048)) * 1024 * 1024

    def _wanted(self, rel: str) -> bool:
        rel = "/" + rel
        if not any(fnmatch.fnmatch(rel, "/" + p) or fnmatch.fnmatch(rel, p) for p in self.include):
            return False
        return not any(fnmatch.fnmatch(rel, "/" + p) or fnmatch.fnmatch(rel, p) for p in self.exclude)

    async def test(self) -> dict[str, Any]:
        if not self.root.is_dir():
            raise PermanentError(f"folder not found: {self.root}")
        count = await asyncio.to_thread(lambda: sum(1 for _ in zip(range(1000), self.root.rglob("*"), strict=False)))
        return {"ok": True, "detail": f"{self.root} is readable ({count}{'+' if count == 1000 else ''} entries)"}

    def _walk(self) -> list[RemoteItem]:
        items: list[RemoteItem] = []
        for dirpath, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(self.root).as_posix()
                if not self._wanted(rel):
                    continue
                try:
                    stat = full.stat()
                except OSError:
                    continue
                if stat.st_size == 0 or stat.st_size > self.max_bytes:
                    continue
                items.append(RemoteItem(
                    external_id=rel, path=rel, name=name, etag=f"{stat.st_mtime_ns}-{stat.st_size}",
                    size=stat.st_size, modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    mime=mimetypes.guess_type(name)[0],
                ))
        return items

    async def changes(self, cursor: dict[str, Any]) -> AsyncIterator[ChangeSet]:
        items = await asyncio.to_thread(self._walk)
        if not items:
            yield ChangeSet(items=[], cursor={"listed_at": datetime.now(UTC).isoformat()}, complete_listing=True,
                            done=True)
            return
        for i in range(0, len(items), PAGE):
            last = i + PAGE >= len(items)
            yield ChangeSet(items=items[i: i + PAGE], cursor={"listed_at": datetime.now(UTC).isoformat()},
                            complete_listing=True, done=last)

    async def download(self, item: RemoteItem) -> bytes:
        path = (self.root / item.external_id).resolve()
        if self.root not in path.parents:
            raise PermanentError("path escapes the source root")
        return await asyncio.to_thread(path.read_bytes)

    async def permissions(self, item: RemoteItem) -> list[str] | None:
        return None

    async def aclose(self) -> None:
        return None
