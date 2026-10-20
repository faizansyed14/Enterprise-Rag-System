"""Built-in guardrails (GUARDRAILS=builtin). LLM Guard / Presidio can replace them (GUARDRAILS=llm_guard) when
installed, but these cover the essentials without extra models:

- questions: length limit, control characters removed, prompt-delimiter tags neutralised, prompt-injection phrases
  flagged (and blocked when several appear together)
- evidence: delimiter tags inside documents are neutralised so a document cannot close the <evidence> block
- logs: e-mail addresses, phone numbers, IBANs and card numbers are masked before anything is written to logs
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_QUESTION_CHARS = 4000

INJECTION_PATTERNS = [
    re.compile(p, re.IGNORECASE) for p in (
        r"\bignore\s+(?:all\s+|any\s+|the\s+)?(?:previous|prior|above|earlier)\s+(?:instructions|rules|prompts?)",
        r"\bdisregard\s+(?:the\s+|your\s+)?(?:system|previous|above)",
        r"\b(?:reveal|show|print|repeat)\s+(?:me\s+)?(?:your|the)\s+(?:system\s+)?(?:prompt|instructions)",
        r"\byou\s+are\s+now\s+(?:a|an|in)\b",
        r"\b(?:developer|god|dan)\s+mode\b",
        r"\bjailbreak",
        r"\bact\s+as\s+(?:an?\s+)?(?:admin|administrator|system)\b",
        r"تجاهل\s+(?:جميع\s+)?التعليمات",
    )
]
_TAGS = re.compile(r"</?\s*(system|evidence|question|conversation|assistant|user)\s*>", re.IGNORECASE)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")


@dataclass
class GuardResult:
    text: str
    blocked: bool = False
    reason: str = ""
    flags: list[str] = field(default_factory=list)


def check_question(question: str) -> GuardResult:
    text = _CONTROL.sub(" ", question or "").strip()
    if not text:
        return GuardResult(text="", blocked=True, reason="The question is empty.")
    if len(text) > MAX_QUESTION_CHARS:
        return GuardResult(text=text[:MAX_QUESTION_CHARS], blocked=True,
                           reason=f"The question is longer than {MAX_QUESTION_CHARS} characters.")
    flags = [p.pattern for p in INJECTION_PATTERNS if p.search(text)]
    text = _TAGS.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text)
    if len(flags) >= 2:
        return GuardResult(text=text, blocked=True, flags=flags,
                           reason="The question looks like an attempt to change the assistant's instructions.")
    return GuardResult(text=text, flags=flags)


def sanitize_evidence(text: str) -> str:
    return _TAGS.sub(lambda m: m.group(0).replace("<", "‹").replace(">", "›"), text)


def mask_pii(text: str) -> str:
    text = _EMAIL.sub("[email]", text)
    text = _IBAN.sub("[iban]", text)
    text = _CARD.sub(lambda m: "[card]" if len(re.sub(r"\D", "", m.group(0))) >= 13 else m.group(0), text)
    return _PHONE.sub(lambda m: "[phone]" if len(re.sub(r"\D", "", m.group(0))) >= 9 else m.group(0), text)
