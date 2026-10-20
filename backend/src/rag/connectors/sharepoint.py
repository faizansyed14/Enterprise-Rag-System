"""`sharepoint` and `onedrive` sources over Microsoft Graph delta queries.

SharePoint config: {"site_url": "https://contoso.sharepoint.com/sites/Projects", "drives": ["Documents"] (optional),
                    "field_map": {"doc_number": "DocumentNumber"} (optional)}
OneDrive config:   {"user": "alex@contoso.com"}

First sync lists everything; later syncs fetch only what changed (delta links per drive). Permissions are read per
item (effective permissions, inherited ones included) and mapped to principals:
  user:<entra object id>  group:<entra group id>  sitegroup:<site id>:<group id>  everyone (organisation-wide links)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

from rag.connectors.base import ChangeSet, RemoteItem, connector_kind
from rag.connectors.graph_client import GraphClient
from rag.errors import PermanentError


def principals_from_permissions(permissions: list[dict[str, Any]], site_id: str | None) -> list[str]:
    out: set[str] = set()
    for perm in permissions:
        link = perm.get("link") or {}
        if link.get("scope") == "organization":
            out.add("everyone")
        if link.get("scope") == "anonymous":
            out.add("everyone")
        identities = list(perm.get("grantedToIdentitiesV2") or [])
        if perm.get("grantedToV2"):
            identities.append(perm["grantedToV2"])
        for identity in identities:
            if (user := identity.get("user")) and user.get("id"):
                out.add(f"user:{user['id']}")
            if (group := identity.get("group")) and group.get("id"):
                out.add(f"group:{group['id']}")
            if (site_group := identity.get("siteGroup")) and site_group.get("id") is not None and site_id:
                out.add(f"sitegroup:{site_id}:{site_group['id']}")
            if (site_user := identity.get("siteUser")) and site_user.get("loginName", "").endswith("spo-grid-all-users"):
                out.add("everyone")
    return sorted(out)


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class _GraphDriveConnector:
    kind: ClassVar[str] = ""

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        self.config = config
        self.settings = settings
        self.graph = GraphClient(settings)
        self.site_id: str | None = None
        self.field_map: dict[str, str] = dict(config.get("field_map") or {})
        self.max_bytes = int(getattr(settings, "max_file_mb", 2048)) * 1024 * 1024

    async def drives(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def test(self) -> dict[str, Any]:
        drives = await self.drives()
        return {"ok": True, "detail": f"{len(drives)} document librar{'y' if len(drives) == 1 else 'ies'}: "
                                      + ", ".join(d.get("name", d["id"]) for d in drives[:8])}

    def _item(self, drive: dict[str, Any], raw: dict[str, Any]) -> RemoteItem | None:
        if raw.get("folder") or raw.get("package"):
            return None
        drive_id = drive["id"]
        external_id = f"{drive_id}:{raw['id']}"
        if raw.get("deleted"):
            return RemoteItem(external_id=external_id, path="", name=raw.get("name", ""), deleted=True)
        if "file" not in raw:
            return None
        parent = (raw.get("parentReference") or {}).get("path", "")
        folder = parent.split("root:", 1)[-1].strip("/") if "root:" in parent else ""
        path = "/".join(p for p in (drive.get("name", ""), folder, raw.get("name", "")) if p)
        size = raw.get("size")
        if size and size > self.max_bytes:
            return None
        return RemoteItem(
            external_id=external_id, path=path, name=raw.get("name", ""),
            etag=raw.get("cTag") or raw.get("eTag"),  # cTag changes with content only; eTag also with metadata
            size=size, modified_at=_parse_time(raw.get("lastModifiedDateTime")),
            mime=(raw.get("file") or {}).get("mimeType"),
        )

    async def changes(self, cursor: dict[str, Any]) -> AsyncIterator[ChangeSet]:
        drives = await self.drives()
        links: dict[str, str] = dict(cursor.get("delta_links") or {})
        for index, drive in enumerate(drives):
            start = links.get(drive["id"]) or f"/drives/{drive['id']}/root/delta"
            async for page in self.graph.pages(start):
                items = [item for raw in page.get("value", []) if (item := self._item(drive, raw))]
                if self.field_map:
                    for item in items:
                        if not item.deleted:
                            item.fields = await self._fields(item)
                if "@odata.deltaLink" in page:
                    links[drive["id"]] = page["@odata.deltaLink"]
                last = index == len(drives) - 1 and "@odata.deltaLink" in page
                yield ChangeSet(items=items, cursor={"delta_links": links}, complete_listing=False, done=last)

    async def _fields(self, item: RemoteItem) -> dict[str, Any]:
        drive_id, item_id = item.external_id.split(":", 1)
        try:
            data = await self.graph.get_json(f"/drives/{drive_id}/items/{item_id}/listItem",
                                             {"$expand": "fields"})
        except PermanentError:
            return {}
        fields = data.get("fields") or {}
        return {ours: fields.get(theirs) for ours, theirs in self.field_map.items() if fields.get(theirs)}

    async def download(self, item: RemoteItem) -> bytes:
        drive_id, item_id = item.external_id.split(":", 1)
        return await self.graph.download(f"/drives/{drive_id}/items/{item_id}/content")

    async def permissions(self, item: RemoteItem) -> list[str] | None:
        drive_id, item_id = item.external_id.split(":", 1)
        perms: list[dict[str, Any]] = []
        async for page in self.graph.pages(f"/drives/{drive_id}/items/{item_id}/permissions"):
            perms.extend(page.get("value", []))
        return principals_from_permissions(perms, self.site_id)

    async def aclose(self) -> None:
        await self.graph.aclose()


@connector_kind("sharepoint")
class SharePointConnector(_GraphDriveConnector):
    kind: ClassVar[str] = "sharepoint"

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        super().__init__(config, settings)
        self.site_url = config.get("site_url", "")
        if not self.site_url and not config.get("site_id"):
            raise PermanentError("sharepoint source needs 'site_url' (or 'site_id')")
        self.site_id = config.get("site_id")
        self.drive_names = set(config.get("drives") or [])

    async def _site(self) -> str:
        if self.site_id:
            return self.site_id
        parsed = urlparse(self.site_url)
        data = await self.graph.get_json(f"/sites/{parsed.hostname}:{quote(parsed.path.rstrip('/'))}")
        self.site_id = data["id"]
        return self.site_id

    async def drives(self) -> list[dict[str, Any]]:
        site = await self._site()
        data = await self.graph.get_json(f"/sites/{site}/drives")
        drives = data.get("value", [])
        if self.drive_names:
            drives = [d for d in drives if d.get("name") in self.drive_names or d.get("id") in self.drive_names]
        return drives


@connector_kind("onedrive")
class OneDriveConnector(_GraphDriveConnector):
    kind: ClassVar[str] = "onedrive"

    def __init__(self, config: dict[str, Any], settings: Any) -> None:
        super().__init__(config, settings)
        self.user = config.get("user", "")
        if not self.user:
            raise PermanentError("onedrive source needs 'user' (UPN or object id)")

    async def drives(self) -> list[dict[str, Any]]:
        drive = await self.graph.get_json(f"/users/{quote(self.user)}/drive")
        return [drive]
