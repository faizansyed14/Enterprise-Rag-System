"""What the connectors brought over from the previous code base expect from their old surroundings.

Principals keep the old spelling inside these modules ("*" for everyone); `rag.connectors.legacy_adapter` translates them to this
system's ("everyone") at the boundary.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

EVERYONE = "*"


def normalize_principal(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    if value in (EVERYONE, "everyone"):
        return EVERYONE
    scheme, sep, rest = value.partition(":")
    if not sep:
        raise ValueError(f"principal {value!r} needs a scheme: user:, group: or sid:")
    scheme = scheme.lower()
    rest = rest.strip()
    if scheme == "sid":
        return f"sid:{rest.upper()}"
    if scheme in ("user", "group", "sitegroup"):
        return f"{scheme}:{rest.lower()}"
    raise ValueError(f"unknown principal scheme {scheme!r}")


def normalize_acl(values: Iterable[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        norm = normalize_principal(value)
        if norm and norm not in out:
            out.append(norm)
    return sorted(out)


@dataclass
class _ScaleSettings:
    max_file_mb: int = 200
    link_scopes_as_access: list[str] = field(default_factory=list)  # sharing links never widen access


def get_scale_settings() -> _ScaleSettings:
    from rag.config.settings import get_settings

    return _ScaleSettings(max_file_mb=int(getattr(get_settings(), "max_file_mb", 200)))
