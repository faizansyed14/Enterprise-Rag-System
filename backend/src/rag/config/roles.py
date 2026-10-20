"""Role lines: every model the system uses is chosen by one `.env` line of the form `<provider>/<model>`.

    LLM_ANSWER=openrouter/anthropic/claude-haiku-5.5      provider=openrouter  model=anthropic/claude-haiku-5.5
    LLM_ANSWER=anthropic/claude-haiku-5-5                 provider=anthropic   model=claude-haiku-5-5
    EMBEDDER=local/multilingual-e5-small                  provider=local       model=multilingual-e5-small
    EMBEDDER=openai/text-embedding-3-large@1024           provider=openai      model=text-embedding-3-large dims=1024
    RERANKER=none                                         provider=none        (role switched off)
    OCR=azure_di/prebuilt-read?pages_per_call=10          options={"pages_per_call": "10"}

The first path segment is always the provider; everything after it is the provider's own model id, which may itself
contain slashes (OpenRouter ids do). `@<n>` sets output dimensions and `?k=v&k2=v2` passes provider options.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import parse_qsl


class Capability(StrEnum):
    CHAT = "chat"
    VISION = "vision"
    EMBEDDER = "embedder"
    RERANKER = "reranker"
    GROUNDEDNESS = "groundedness"
    PARSER = "parser"
    OCR = "ocr"


# Role name -> what the role does. Order is the order the Models page shows them in.
ROLES: dict[str, Capability] = {
    "LLM_ANSWER": Capability.CHAT,
    "LLM_ANSWER_HARD": Capability.CHAT,
    "LLM_ANSWER_FALLBACK": Capability.CHAT,
    "LLM_ROUTER": Capability.CHAT,
    "LLM_ENRICH": Capability.CHAT,
    "VISION": Capability.VISION,
    "EMBEDDER": Capability.EMBEDDER,
    "RERANKER": Capability.RERANKER,
    "GROUNDEDNESS": Capability.GROUNDEDNESS,
    "PARSER": Capability.PARSER,
    "OCR": Capability.OCR,
}

ROLE_DESCRIPTIONS: dict[str, str] = {
    "LLM_ANSWER": "Writes answers for everyday questions",
    "LLM_ANSWER_HARD": "Writes answers for multi-part, comparison and cross-document questions",
    "LLM_ANSWER_FALLBACK": "Takes over when the answer model fails or times out",
    "LLM_ROUTER": "Rewrites follow-ups, translates queries and reads filters from questions",
    "LLM_ENRICH": "Fills metadata gaps (title, type) during ingestion",
    "VISION": "Reads drawing sheets and pages that text extraction cannot",
    "EMBEDDER": "Turns chunks and questions into dense vectors",
    "RERANKER": "Re-scores the top candidates with a cross-encoder",
    "GROUNDEDNESS": "Checks that each answer sentence is supported by its cited passage",
    "PARSER": "Reads PDF pages (text layer, tables, layout)",
    "OCR": "Reads scanned pages, tables and pictures: with the local OCR, or with the vision model (better on tables and handwriting, a model call per page)",
}

# Roles that may be switched from the UI at runtime: the chat and vision models, and how scanned pages are read (which applies
# to files uploaded afterwards; files already stored need `rag reparse`). Everything else changes the index and is switched in
# `.env` with a rebuild, so that switch is deliberate.
UI_SWITCHABLE_ROLES = frozenset({"LLM_ANSWER", "LLM_ANSWER_HARD", "LLM_ANSWER_FALLBACK", "LLM_ROUTER", "LLM_ENRICH", "VISION", "OCR"})

# Providers that run inside this deployment (no data leaves it).
LOCAL_PROVIDERS = frozenset({"local", "hashing", "extractive", "lexical", "builtin", "none", "openai_compat"})


class RoleSpecError(ValueError):
    pass


@dataclass(frozen=True)
class RoleSpec:
    role: str
    provider: str
    model: str
    dims: int | None = None
    options: dict[str, str] = field(default_factory=dict)

    @property
    def capability(self) -> Capability:
        return ROLES[self.role]

    @property
    def is_off(self) -> bool:
        return self.provider == "none"

    @property
    def is_local(self) -> bool:
        return self.provider in LOCAL_PROVIDERS

    @property
    def label(self) -> str:
        if self.is_off:
            return "none"
        text = f"{self.provider}/{self.model}"
        if self.dims:
            text += f"@{self.dims}"
        return text

    def with_model(self, line: str) -> RoleSpec:
        return parse_role(self.role, line)

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "capability": self.capability.value,
            "provider": self.provider,
            "model": self.model,
            "dims": self.dims,
            "options": dict(self.options),
            "label": self.label,
            "local": self.is_local,
        }


def parse_role(role: str, line: str) -> RoleSpec:
    """Parse one role line. Raises RoleSpecError with a message that names the role and shows the expected form."""
    role = role.strip().upper()
    if role not in ROLES:
        raise RoleSpecError(f"Unknown role {role!r}. Known roles: {', '.join(ROLES)}")
    value = (line or "").strip()
    if not value:
        raise RoleSpecError(f"{role} is empty. Expected <provider>/<model>, for example local/multilingual-e5-small")
    if value.lower() in {"none", "off", "disabled"}:
        return RoleSpec(role=role, provider="none", model="")

    options: dict[str, str] = {}
    if "?" in value:
        value, query = value.split("?", 1)
        options = {k.strip(): v.strip() for k, v in parse_qsl(query, keep_blank_values=True)}

    if "/" not in value:
        raise RoleSpecError(
            f"{role}={line!r} has no provider. Expected <provider>/<model>, for example openrouter/anthropic/claude-haiku-5.5"
        )
    provider, model = value.split("/", 1)
    provider = provider.strip().lower()
    model = model.strip()

    dims: int | None = None
    if "@" in model:
        model, dims_text = model.rsplit("@", 1)
        if not dims_text.isdigit() or int(dims_text) <= 0:
            raise RoleSpecError(f"{role}={line!r}: '@{dims_text}' must be a positive number of dimensions")
        dims = int(dims_text)

    if not provider or not model:
        raise RoleSpecError(f"{role}={line!r}: both provider and model are required")
    if any(ch.isspace() for ch in model):
        raise RoleSpecError(f"{role}={line!r}: model ids cannot contain spaces")
    return RoleSpec(role=role, provider=provider, model=model, dims=dims, options=options)


def parse_choices(line: str) -> list[str]:
    """MODEL_CHOICES: comma-separated role lines users may pick from in the chat composer."""
    items = [item.strip() for item in (line or "").split(",")]
    return [item for item in items if item]
