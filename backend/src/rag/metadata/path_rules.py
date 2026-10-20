"""Metadata from folder paths.

Default rules read project and document type from folder names. A YAML file (PATH_RULES_FILE) adds
site-specific rules; each rule is a regular expression on the full path with named groups or fixed values:

    rules:
      - match: '(?i)^/?Projects/(?P<project>P\\d{4})[^/]*/'
      - match: '(?i)/02[_ -]Structural/'
        set: {doc_type: drawing}
      - match: '(?i)/Archive/'
        set: {status: archived}
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from rag.metadata.vocabularies import doc_type_from_text

_PROJECT_CODE = re.compile(r"(?i)^(?:P|PRJ|PROJ|JOB)[-_ ]?\d{2,6}[A-Z]?\b|^\d{4,6}[-_ ][A-Za-z]")


@dataclass
class PathRule:
    pattern: re.Pattern[str]
    values: dict[str, str] = field(default_factory=dict)


class PathRules:
    def __init__(self, rules: list[PathRule] | None = None, known_projects: list[str] | None = None) -> None:
        self.rules = rules or []
        self.known_projects = {p.lower(): p for p in (known_projects or [])}

    @classmethod
    def load(cls, path: str | None, known_projects: list[str] | None = None) -> PathRules:
        rules: list[PathRule] = []
        if path:
            data: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
            rules.extend(PathRule(re.compile(entry["match"]), {k: str(v) for k, v in (entry.get("set") or {}).items()})
                         for entry in data.get("rules", []))
            known_projects = (known_projects or []) + [str(p) for p in data.get("projects", [])]
        return cls(rules, known_projects)

    def apply(self, path: str) -> dict[str, tuple[str, float]]:
        """{field: (value, confidence)} from the folder path (file name excluded)."""
        normalized = "/" + path.replace("\\", "/").strip("/")
        folders = [p for p in normalized.split("/")[:-1] if p]
        out: dict[str, tuple[str, float]] = {}

        for rule in self.rules:
            m = rule.pattern.search(normalized)
            if not m:
                continue
            for name, value in m.groupdict().items():
                if value:
                    out.setdefault(name, (value.strip(), 0.9))
            for name, value in rule.values.items():
                out.setdefault(name, (value.format(**m.groupdict()), 0.9))

        if "project" not in out:
            for folder in folders:
                known = self.known_projects.get(folder.lower())
                if known:
                    out["project"] = (known, 0.9)
                    break
            else:
                for i, folder in enumerate(folders):
                    if _PROJECT_CODE.match(folder):
                        out["project"] = (folder, 0.6)
                        break
                    if folder.lower() in {"projects", "project", "المشاريع"} and i + 1 < len(folders):
                        out["project"] = (folders[i + 1], 0.6)
                        break
        for folder in reversed(folders):
            text = re.sub(r"^\d+[\s._-]+", "", folder.replace("_", " "))
            if "doc_type" not in out and (t := doc_type_from_text(text)):
                out["doc_type"] = (t, 0.55)
        return out
