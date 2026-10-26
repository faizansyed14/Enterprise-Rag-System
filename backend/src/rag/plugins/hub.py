"""The models in use right now, one per role.

`.env` decides every role. Chat roles (LLM_ANSWER, LLM_ANSWER_HARD, LLM_ROUTER) can additionally be switched from the
Models page when ALLOW_UI_MODEL_SWITCH=true; the switch is stored in Postgres (role_overrides, with an audit row in
model_switches) and picked up by every API process within a few seconds. Users can pick per question from
MODEL_CHOICES in the chat composer.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, ClassVar

from sqlalchemy import select

from rag.config.roles import ROLES, UI_SWITCHABLE_ROLES, Capability, RoleSpec, parse_role
from rag.config.settings import Settings
from rag.errors import ConfigurationError
from rag.plugins.registry import PluginError, load_plugins, registry

log = logging.getLogger(__name__)

LOCAL_ANSWERER = "extractive/default"  # sentences taken from the passages: no model, no cost, nothing leaves the machine

OVERRIDE_REFRESH_S = 5.0


class ModelHub:
    def __init__(self, settings: Settings) -> None:
        load_plugins()
        self.settings = settings
        self._instances: dict[tuple[str, str], Any] = {}
        self._overrides: dict[str, str] = {}
        self._overrides_loaded_at = 0.0
        self._lock = asyncio.Lock()
        self._unusable_logged: set[tuple[str, str]] = set()

    # ---- which spec applies ------------------------------------------------------------------------------------------
    def env_spec(self, role: str) -> RoleSpec:
        return self.settings.role(role)

    def spec(self, role: str) -> RoleSpec:
        override = self._overrides.get(role)
        if override and self.settings.allow_ui_model_switch and role in UI_SWITCHABLE_ROLES:
            try:
                return parse_role(role, override)
            except ValueError:
                log.warning("ignoring invalid override for %s: %r", role, override)
        return self.env_spec(role)

    def specs(self) -> dict[str, RoleSpec]:
        return {role: self.spec(role) for role in ROLES}

    async def refresh_overrides(self, *, force: bool = False) -> None:
        if not self.settings.allow_ui_model_switch:
            self._overrides = {}
            return
        if not force and time.monotonic() - self._overrides_loaded_at < OVERRIDE_REFRESH_S:
            return
        from rag.db import schema
        from rag.db.engine import connection

        try:
            async with connection() as conn:
                rows = (await conn.execute(select(schema.role_overrides.c.role, schema.role_overrides.c.spec))).all()
            self._overrides = {r[0]: r[1] for r in rows}
        except Exception:
            log.warning("could not refresh model overrides", exc_info=True)
        self._overrides_loaded_at = time.monotonic()

    # ---- instances -----------------------------------------------------------------------------------------------------
    def get(self, role: str, line: str | None = None) -> Any:
        spec = parse_role(role, line) if line else self.spec(role)
        if spec.is_off:
            return None
        key = (role, spec.label, tuple(sorted(spec.options.items())))  # the same model with other options is another instance
        instance = self._instances.get(key)
        if instance is None:
            if role == "OCR" and spec.provider == "vision":
                # OCR by the vision model needs the hub (it uses the VISION role and falls back to the local OCR)
                from rag.providers.ocr.vision import VisionOCR

                if self.spec("VISION").is_off:
                    raise ConfigurationError("OCR=vision/default needs a model for 'Drawing and image reading' (VISION)")
                instance = VisionOCR(self)
            else:
                try:
                    instance = registry.create(spec, self.settings)
                except PluginError as exc:
                    raise ConfigurationError(str(exc)) from exc
                if (self.settings.privacy_mask_for_models and not spec.is_local
                        and spec.capability in (Capability.CHAT, Capability.VISION)):
                    from rag.privacy.shield import ShieldedChat

                    instance = ShieldedChat(instance, self.settings.privacy_kind_list)
            self._instances[key] = instance
        return instance

    def try_get(self, role: str) -> Any:
        try:
            return self.get(role)
        except ConfigurationError as exc:
            # Optional roles fall back quietly; say so once per role and model, not on every page or question.
            key = (role, self.spec(role).label)
            if key not in self._unusable_logged:
                self._unusable_logged.add(key)
                log.warning("%s is not usable, continuing without it: %s", role, exc)
            return None

    def chat(self, role: str = "LLM_ANSWER", choice: str | None = None) -> Any:
        if choice:
            allowed = set(self.settings.model_choice_lines) | {self.spec(role).label, LOCAL_ANSWERER}
            if choice not in allowed and not self._any_openrouter(choice):
                raise ConfigurationError(f"{choice} is not in MODEL_CHOICES")
            return self.get(role, choice)
        return self.get(role)

    def _any_openrouter(self, choice: str) -> bool:
        """`openrouter/<vendor>/<model>` for any model in OpenRouter's catalogue (any well-formed id until the catalogue is
        known), when ALLOW_ANY_OPENROUTER_MODEL is on."""
        from rag.providers import openrouter_catalog

        if not self.settings.allow_any_openrouter_model or not choice.startswith("openrouter/"):
            return False
        model_id = choice.removeprefix("openrouter/")
        known = openrouter_catalog.known_ids()
        if not openrouter_catalog.allows(model_id, self.settings.openrouter_models):
            return False
        return model_id in known if known else model_id.count("/") >= 1 and " " not in model_id

    def embedder(self) -> Any:
        embedder = self.get("EMBEDDER")
        if embedder is None:
            raise ConfigurationError("EMBEDDER cannot be none")
        return embedder

    def reranker(self) -> Any:
        return self.try_get("RERANKER")

    def groundedness(self) -> Any:
        return self.try_get("GROUNDEDNESS")

    def ocr(self) -> Any:
        return self.try_get("OCR")

    def vision(self) -> Any:
        """The vision model; with VISION_FALLBACK set, a second model takes over any request the first one fails (HTTP 429)."""
        primary = self.try_get("VISION")
        line = self.settings.vision_fallback.strip()
        if primary is None or not line or line == self.spec("VISION").label:
            return primary
        key = ("VISION", "with fallback", self.spec("VISION").label, line)
        wrapped = self._instances.get(key)
        if wrapped is None:
            from rag.providers.ocr.vision import FallbackVision

            try:
                backup = self.get("VISION", line)
            except (ConfigurationError, ValueError) as exc:
                if key not in self._unusable_logged:
                    self._unusable_logged.add(key)
                    log.warning("VISION_FALLBACK %s is not usable, continuing without it: %s", line, exc)
                backup = None
            wrapped = primary if backup is None else FallbackVision(primary, backup)
            self._instances[key] = wrapped
        return wrapped

    def cad_options(self) -> Any:
        """How AutoCAD sheets are analysed (rag/ingestion/cad_analysis.py): the CAD_VISION role (.env, or what the Models page
        switched it to) and the CAD_* settings. None, and the sheets get the ordinary short description, when the role is none
        or cannot be built (no key)."""
        from rag.ingestion.cad_analysis import Options

        from urllib.parse import urlencode

        s = self.settings
        spec = self.spec("CAD_VISION")
        if spec.is_off:
            return None
        # A report of several thousand words is written after the model's hidden reasoning, and both count against the output
        # limit: measured on a site plan, 14,700 of 16,000 tokens went on thinking and the report was cut off in its tenant
        # table. OpenRouter is asked for low reasoning (CAD_REASONING; empty = the model's own default).
        options = dict(spec.options)
        effort = (getattr(s, "cad_reasoning", "") or "").strip().lower()
        if effort and spec.provider == "openrouter":
            options.setdefault("reasoning", effort)
        line = spec.label + (f"?{urlencode(options)}" if options else "")
        try:
            model = self.get("CAD_VISION", line)
        except (ConfigurationError, ValueError) as exc:
            key = ("CAD_VISION", spec.label)
            if key not in self._unusable_logged:
                self._unusable_logged.add(key)
                log.warning("CAD_VISION is not usable, AutoCAD sheets get the short description: %s", exc)
            return None
        return Options(model=model, tiles=s.cad_tiles, render_px=s.cad_render_px, max_sheets=s.cad_max_sheets,
                       send_text=s.cad_send_text, max_tokens=s.cad_max_tokens, colour=s.cad_colour,
                       timeout_s=s.cad_timeout_s) if model else None

    def parser(self) -> Any:
        """The PDF parser with OCR, vision, OCR cache and budgets attached."""
        from rag.ingestion.ocr_cache import cache_get, cache_put
        from rag.jobs.rate_limits import take_budget
        from rag.privacy.shield import bank_kinds

        parser = self.get("PARSER")
        if parser is None:
            raise ConfigurationError("PARSER cannot be none")
        inner = getattr(parser, "inner", parser)
        if hasattr(inner, "attach"):
            ocr_spec = self.spec("OCR")
            # cached page text belongs to the model that read it: switching the vision model must not reuse another's pages
            ocr_label = f"vision:{self.spec('VISION').label}" if ocr_spec.provider == "vision" else ocr_spec.label
            limits = {"cloud_ocr_pages": self.settings.cloud_ocr_pages_per_day,
                      "vision_pages": self.settings.vision_pages_per_day}

            async def budget(bucket: str, amount: int) -> bool:
                return await take_budget(bucket, amount, limits.get(bucket, 0))

            inner.attach(ocr=self.ocr(), ocr_label=ocr_label, ocr_is_cloud=not ocr_spec.is_local,
                         vision=self.vision(), cache_get=cache_get, cache_put=cache_put, take_budget=budget,
                         protect_kinds=(bank_kinds(self.settings.privacy_kind_list)
                                        if self.settings.privacy_mask_for_models else None),
                         cad=self.cad_options())
        return parser

    OCR_MODES: ClassVar[dict[str, str]] = {"local": "local/rapidocr", "vision": "vision/default"}

    def reading(self, ocr_mode: str | None = None) -> tuple[Any, Any]:
        """(PDF parser, OCR engine) to read one file.

        None: what the Models page says. "local": OCR on this server and no vision model at all, so nothing leaves it.
        "vision": the vision model reads scans and images (the local OCR still checks each image for bank or id numbers
        first). A chosen mode gets its own parser object, because attaching engines changes the parser and other files are
        being read at the same moment; the OCR cache is not consulted, so the file really is read again."""
        if ocr_mode is None:
            return self.parser(), self.ocr()
        if ocr_mode not in self.OCR_MODES:
            raise ConfigurationError(f"unknown OCR mode {ocr_mode!r}; use local or vision")
        from rag.ingestion.ocr_cache import cache_put
        from rag.jobs.rate_limits import take_budget
        from rag.privacy.shield import bank_kinds

        if ocr_mode == "vision" and self.spec("VISION").is_off:
            raise ConfigurationError(
                "LLM OCR needs a vision model: choose one for 'Drawing and image reading' on the Models page"
            )
        ocr = self.get("OCR", self.OCR_MODES[ocr_mode])
        vision = self.vision() if ocr_mode == "vision" else None
        parser = registry.create(self.spec("PARSER"), self.settings)
        inner = getattr(parser, "inner", parser)
        if hasattr(inner, "attach"):
            ocr_label = f"vision:{self.spec('VISION').label}" if ocr_mode == "vision" else self.OCR_MODES["local"]
            limits = {
                "cloud_ocr_pages": self.settings.cloud_ocr_pages_per_day,
                "vision_pages": self.settings.vision_pages_per_day,
            }

            async def budget(bucket: str, amount: int) -> bool:
                return await take_budget(bucket, amount, limits.get(bucket, 0))

            async def no_cache(image_hash: str, engine: str) -> None:
                return None

            inner.attach(
                ocr=ocr,
                ocr_label=ocr_label,
                ocr_is_cloud=ocr_mode == "vision",
                vision=vision,
                cache_get=no_cache,
                cache_put=cache_put,
                take_budget=budget,
                protect_kinds=bank_kinds(self.settings.privacy_kind_list) if self.settings.privacy_mask_for_models else None,
                cad=self.cad_options() if ocr_mode == "vision" else None,  # "local" means nothing leaves this server
            )
        return parser, ocr

    async def aclose(self) -> None:
        for instance in list(self._instances.values()):
            close = getattr(instance, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    log.debug("error closing plugin", exc_info=True)
        self._instances.clear()

    # ---- status for the Models page -------------------------------------------------------------------------------------
    def describe(self) -> list[dict[str, Any]]:
        from rag.config.roles import ROLE_DESCRIPTIONS

        rows = []
        for role, spec in self.specs().items():
            info = registry.info(spec.capability, spec.provider) if not spec.is_off else None
            env = self.env_spec(role)
            rows.append(
                {
                    **spec.as_dict(),
                    "description": ROLE_DESCRIPTIONS.get(role, ""),
                    "provider_description": info.description if info else "",
                    "known_provider": spec.is_off or info is not None,
                    "missing": registry.missing_requirements(spec, self.settings) if info else [],
                    "overridden": spec.label != env.label,
                    "env_label": env.label,
                    "switchable": role in UI_SWITCHABLE_ROLES and self.settings.allow_ui_model_switch,
                }
            )
        return rows
