"""Golden sets: questions with the documents, pages and facts a correct answer must use.

Built-in `demo` matches the demo corpus. Your own sets are YAML files in EVAL_DIR (default ./eval), one per set:

    - id: column-grade
      question: What concrete grade is required for the columns on Level 3?
      expect:
        documents: ["03 30 00"]          # document numbers (any spelling) or parts of file names
        pages: [1]                       # optional: a page that must be retrieved
        answer_contains: ["C40"]         # facts the answer must state
        answer_not_contains: ["C35"]     # facts it must not state (wrong values)
        not_found: false                 # true when the library does not answer the question
      principals: ["everyone"]           # optional: whose access to search with
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

DEMO: list[dict[str, Any]] = [
    {"id": "column-grade", "question": "What concrete grade is required for the columns on Level 3?",
     "expect": {"documents": ["03 30 00"], "pages": [1], "answer_contains": ["C40"],
                "answer_not_contains": ["C35"]}},
    {"id": "hot-weather", "question": "What is the maximum ambient temperature for placing concrete?",
     "expect": {"documents": ["03 30 00"], "pages": [2], "answer_contains": ["35"]}},
    {"id": "cover", "question": "What concrete cover is required for columns?",
     "expect": {"documents": ["03 30 00"], "pages": [2], "answer_contains": ["50 mm"]}},
    {"id": "rfi-response", "question": "What was the engineer's response to RFI-023?",
     "expect": {"documents": ["RFI-023"], "answer_contains": ["600"]}},
    {"id": "site-instruction", "question": "What does site instruction SI-014 require on Level 3?",
     "expect": {"documents": ["SI-014", "SI-14"], "answer_contains": ["fire dampers"]}},
    {"id": "drawing-title", "question": "What is the title of drawing PRJ1-ABC-ZZ-03-DR-S-2001?",
     "expect": {"documents": ["PRJ1-ABC-ZZ-03-DR-S-2001"], "answer_contains": ["COLUMN LAYOUT"]}},
    {"id": "column-size", "question": "What size are the columns on Level 03 according to the structural drawing?",
     "expect": {"documents": ["PRJ1-ABC-ZZ-03-DR-S-2001"], "answer_contains": ["600"]}},
    {"id": "formwork-owner", "question": "Who must inspect the Level 3 column formwork and by when?",
     "expect": {"documents": ["Minutes of Meeting 14"], "answer_contains": ["Ahmed Saleh"]}},
    {"id": "arabic-pour", "question": "متى موعد صب خرسانة سقف الطابق الثالث؟",
     "expect": {"documents": ["محضر اجتماع 15"], "answer_contains": ["15 أبريل 2025"]}},
    {"id": "boq-rebar", "question": "What quantity of 16mm reinforcement steel is in the bill of quantities?",
     "expect": {"documents": ["BOQ Structural Works"], "answer_contains": ["420"]}},
    {"id": "progress", "question": "What was the overall progress reported for March 2025?",
     "expect": {"documents": ["Progress Report March 2025"], "answer_contains": ["42%"]}},
    {"id": "submittal-status", "question": "Was submittal SUB-084 approved, and with what comment?",
     "expect": {"documents": ["SUB-084"], "answer_contains": ["50 diameters"]}},
    {"id": "other-project", "question": "What concrete grade is specified for columns in Harbour View?",
     "expect": {"documents": ["03 30 00"], "project": "Harbour View", "answer_contains": ["C50"],
                "answer_not_contains": ["C40"]}},
    {"id": "restricted", "question": "What is the agreed value of variation order VO-15?",
     "expect": {"not_found": True, "answer_not_contains": ["1,450,000"]}},
    {"id": "unanswerable", "question": "What is the warranty period for the passenger lifts?",
     "expect": {"not_found": True}},
]


def eval_dir() -> Path:
    return Path(os.environ.get("EVAL_DIR", "./eval"))


def available_sets() -> list[str]:
    folder = eval_dir()
    files = sorted(p.stem for p in folder.glob("*.yaml")) if folder.is_dir() else []
    return ["demo", *files]


def load_set(name: str) -> list[dict[str, Any]]:
    if name == "demo":
        return [dict(item) for item in DEMO]
    path = eval_dir() / f"{name}.yaml"
    if not path.is_file() or path.resolve().parent != eval_dir().resolve():
        raise FileNotFoundError(f"golden set {name!r} not found in {eval_dir()}")
    items = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    for i, item in enumerate(items):
        item.setdefault("id", f"q{i + 1}")
        item.setdefault("expect", {})
    return items
