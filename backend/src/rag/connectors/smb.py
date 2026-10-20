"""`smb` source: a Windows file share read directly over SMB 2/3 (no mount needed).

Config: {"server": "fs01.corp.local", "share": "Projects", "path": "Active", "username": "svc-rag",
         "password_env": "SMB_PASSWORD", "include": [...], "exclude": [...]}
The password is read from the environment variable named in `password_env`; it is never stored in the database.
Items use the source's default ACL (NTFS ACLs are not mapped to Entra principals).
"""

from __future__ import annotations

import asyncio
import fnmatch
import mimetypes
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, ClassVar

from rag.connectors.base import ChangeSet, RemoteItem, connector_kind
from rag.connectors.filesystem import DEFAULT_EXCLUDE, PAGE
from rag.errors import PermanentError


@connector_kind("smb")
class SMBConnector:
    kind: ClassVar[str] = "smb"

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        self.server = config.get("server") or ""
        self.share = config.get("share") or ""
        if not self.server or not self.share:
            raise PermanentError("smb source needs 'server' and 'share'")
        self.base = config.get("path", "").strip("\\/")
        self.username = config.get("username")
        self.password = os.environ.get(config.get("password_env", "SMB_PASSWORD"), "")
        self.include = config.get("include") or ["**/*"]
        self.exclude = DEFAULT_EXCLUDE + list(config.get("exclude") or [])
        self.max_bytes = int(getattr(settings, "max_file_mb", 2048)) * 1024 * 1024
        self._registered = False

    def _unc(self, rel: str = "") -> str:
        parts = [f"\\\\{self.server}\\{self.share}"]
        if self.base:
            parts.append(self.base.replace("/", "\\"))
        if rel:
            parts.append(rel.replace("/", "\\"))
        return "\\".join(parts)

    def _session(self) -> Any:
        import smbclient

        if not self._registered:
            smbclient.register_session(self.server, username=self.username, password=self.password,
                                       require_signing=True)
            self._registered = True
        return smbclient

    def _wanted(self, rel: str) -> bool:
        return (any(fnmatch.fnmatch(rel, p) for p in self.include)
                and not any(fnmatch.fnmatch(rel, p) for p in self.exclude))

    def _walk(self) -> list[RemoteItem]:
        client = self._session()
        items: list[RemoteItem] = []
        stack = [""]
        while stack:
            rel_dir = stack.pop()
            for entry in client.scandir(self._unc(rel_dir)):
                rel = f"{rel_dir}/{entry.name}".lstrip("/")
                if entry.is_dir():
                    if not entry.name.startswith("."):
                        stack.append(rel)
                    continue
                if not self._wanted(rel):
                    continue
                stat = entry.stat()
                if stat.st_size == 0 or stat.st_size > self.max_bytes:
                    continue
                items.append(RemoteItem(
                    external_id=rel, path=rel, name=entry.name, etag=f"{int(stat.st_mtime * 1e9)}-{stat.st_size}",
                    size=stat.st_size, modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
                    mime=mimetypes.guess_type(entry.name)[0]))
        return items

    async def test(self) -> dict[str, Any]:
        client = self._session()
        entries = await asyncio.to_thread(lambda: list(client.scandir(self._unc()))[:20])
        return {"ok": True, "detail": f"{self._unc()} is readable ({len(entries)} entries at the top level)"}

    async def changes(self, cursor: dict[str, Any]) -> AsyncIterator[ChangeSet]:
        items = await asyncio.to_thread(self._walk)
        stamp = {"listed_at": datetime.now(UTC).isoformat()}
        if not items:
            yield ChangeSet(items=[], cursor=stamp, complete_listing=True, done=True)
            return
        for i in range(0, len(items), PAGE):
            yield ChangeSet(items=items[i: i + PAGE], cursor=stamp, complete_listing=True,
                            done=i + PAGE >= len(items))

    async def download(self, item: RemoteItem) -> bytes:
        client = self._session()

        def read() -> bytes:
            with client.open_file(self._unc(item.external_id), mode="rb") as fh:
                return fh.read()

        return await asyncio.to_thread(read)

    async def permissions(self, item: RemoteItem) -> list[str] | None:
        return None

    async def aclose(self) -> None:
        if self._registered:
            import smbclient

            await asyncio.to_thread(smbclient.reset_connection_cache)
