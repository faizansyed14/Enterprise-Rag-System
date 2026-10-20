"""Connector contract. A connector lists changes since a cursor, downloads content and reports permissions.

Adding a source system means implementing this class and registering it in CONNECTORS; nothing else changes.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar, Protocol


@dataclass
class RemoteItem:
    external_id: str
    path: str
    name: str
    etag: str | None = None
    size: int | None = None
    modified_at: datetime | None = None
    mime: str | None = None
    acl: list[str] | None = None  # principals; None means "use the source's default ACL"
    fields: dict[str, Any] = field(default_factory=dict)  # source columns (document number ...)
    deleted: bool = False


@dataclass
class ChangeSet:
    items: list[RemoteItem]
    cursor: dict[str, Any]
    complete_listing: bool = False  # True: this sync lists everything, so unseen items were deleted
    done: bool = False


def acl_hash(acl: list[str] | None) -> str:
    return hashlib.sha256("\n".join(sorted(set(acl or []))).encode()).hexdigest()[:16]


class Connector(Protocol):
    kind: ClassVar[str]

    async def test(self) -> dict[str, Any]:
        """Check credentials and reachability; return a short description for the Sources page."""
        ...

    def changes(self, cursor: dict[str, Any]) -> AsyncIterator[ChangeSet]:
        """Pages of changes since `cursor`. The last page carries the cursor for the next sync and done=True."""
        ...

    async def download(self, item: RemoteItem) -> bytes: ...

    async def permissions(self, item: RemoteItem) -> list[str] | None:
        """Current principals for one item (None: not supported, keep the default ACL)."""
        ...

    async def aclose(self) -> None: ...


CONNECTORS: dict[str, Any] = {}


def connector_kind(kind: str):
    def decorator(cls: Any) -> Any:
        CONNECTORS[kind] = cls
        return cls

    return decorator


def _load_builtin() -> None:
    # Import the built-in connectors so they register themselves.
    from rag.connectors import filesystem, legacy_adapter, sharepoint, smb, upload  # noqa: F401


def known_kinds() -> list[str]:
    _load_builtin()
    return sorted(CONNECTORS)


def make_connector(kind: str, config: dict[str, Any], settings: Any) -> Connector:
    _load_builtin()
    cls = CONNECTORS.get(kind)
    if cls is None:
        raise ValueError(f"unknown source kind {kind!r}; known: {', '.join(sorted(CONNECTORS))}")
    return cls(config, settings)
