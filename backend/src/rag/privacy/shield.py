"""Keep bank accounts, cards and personal ids away from outside models.

Everything that reaches a cloud chat or vision model is built in one place, so that is where the protection sits:

  find(text)         the sensitive values in a text (IBANs, card numbers, labelled bank account numbers, SWIFT/BIC,
                     sort and routing codes, national ids), checked where a check exists (IBAN mod 97, card Luhn)
                     so ordinary numbers are left alone
  Vault              swaps each value for a placeholder such as [[ACCOUNT_1]] before a request leaves, and swaps the
                     real value back into the reply, so the answer a person reads still has the number while the
                     model only ever saw the placeholder
  ShieldedChat       wraps a chat model with that round trip: messages, tool-call arguments, and a streamed reply
                     that may split a placeholder over several pieces
  redact(text)       permanent removal, for people who do not want the values in the index at all

Images cannot be masked, so a page whose own text contains such a value is not sent as an image (see providers/ocr/vision.py).
Only this process holds the mapping between placeholders and values, and only for the duration of one request.
"""

from __future__ import annotations

import functools
import logging
import re
from collections.abc import AsyncIterator, Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any

from rag.plugins.capabilities import ChatMessage, ChatResult, StreamChunk, ToolCall

log = logging.getLogger(__name__)

# Set by ShieldedChat while a call runs: None = no shield on this call, n = the shield was on and masked n values. The
# metering layer stores it with the call (spans.attrs.masked_values), so `rag privacy calls` can show it. Only a count
# is kept, never a value.
MASKED_VALUES: ContextVar[int | None] = ContextVar("masked_values", default=None)

BANK_KINDS = (
    "iban",
    "card",
    "account",
    "swift",
    "ssn",
    "emirates_id",
    "passport",
    "payment_ref",
    "secret",
    "tax_id",
)  # money, identity and credentials: these keep a page or a whole document away
ALL_KINDS = (*BANK_KINDS, "email", "phone", "long_number", "custom")  # ... whereas these are only masked in text


def bank_kinds(kinds: Sequence[str]) -> tuple[str, ...]:
    """The kinds among `kinds` that are worth keeping a whole page or document away from a cloud model."""
    return tuple(k for k in kinds if k in BANK_KINDS)


# groups may be separated by spaces or a line break: an IBAN that wraps at the end of a line is still one IBAN
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:\s{0,3}[A-Z0-9]{4}){2,7}(?:\s{0,3}[A-Z0-9]{1,3})?\b", re.IGNORECASE)
_CARD = re.compile(r"(?<![\d-])(?:\d[ -]?){12,18}\d(?![\d-])")
_ACCOUNT_LABEL = (
    r"(?:(?:bank\s+|beneficiary\s+|savings\s+|current\s+|salary\s+)?(?:account|acct|a/c)"
    r"(?:\s*(?:no\.?|number|num\.?|nbr|#|id))?|sort\s*code|routing(?:\s*(?:no\.?|number|#))?|aba(?:\s*(?:no\.?|number))?|ifsc"
    r"|clabe|bsb|iban"
    # Arabic: "رقم الحساب" (account number), "حساب رقم", "رقم الحساب المصرفي", "الآيبان" (IBAN)
    r"|(?:رقم\s+)?(?:الحساب|حساب)(?:\s+(?:المصرفي|البنكي))?(?:\s+رقم)?|(?:رقم\s+)?(?:الآيبان|الايبان|آيبان|ايبان))"
)
# up to three short words between the label and its value: "Account Number AED: ...", "Account No. (USD) -
# ...", "A/C Number Current: ..."
_CURRENCIES = "AED|USD|EUR|GBP|SAR|QAR|KWD|BHD|OMR|INR|PKR|EGP|JOD|LBP|CHF|JPY|CNY|CAD|AUD|SGD|HKD|NZD|ZAR|TRY"
_QUALIFIERS = (
    rf"(?:[ \t]+[(\[]?(?:(?:{_CURRENCIES})(?:[/,&](?:{_CURRENCIES}))*|current|savings?|salary|main|primary|new|old|local|foreign|"
    r"corporate|business|personal)[)\]]?){0,2}"
)
_ACCOUNT = re.compile(
    rf"\b{_ACCOUNT_LABEL}\b\.?{_QUALIFIERS}[ \t]*[:=\-–|#.]*\s*((?:[A-Z]{{1,4}}[ -]?)?\d[\d ./-]{{4,30}}\d)", re.IGNORECASE
)
_SWIFT = re.compile(
    r"\b(?:swift|bic)(?:\s*(?:code|/\s*bic))?\b[\s:=\-–|#.]*([A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?)\b", re.IGNORECASE
)
_SSN = re.compile(r"\b(?!000|666|9\d\d)\d{3}-(?!00)\d{2}-(?!0000)\d{4}\b")
_EMIRATES_ID = re.compile(r"\b784[- ]?\d{4}[- ]?\d{7}[- ]?\d\b")
_EMAIL = re.compile(
    r"(?<![\w.+-])[A-Z0-9][A-Z0-9._%+-]*@[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?(?:\.[A-Z0-9](?:[A-Z0-9-]*[A-Z0-9])?)*\.[A-Z]{2,}(?![\w-])",
    re.IGNORECASE,
)
_PHONE_LABEL = r"(?:tel(?:ephone)?|phone|mobile|mob|cell|whatsapp|fax|ph|contact(?:\s*(?:no\.?|number))?)"
# international form (+971 50 123 4567), a labelled number, or the UAE shapes: mobile 05x xxx xxxx and landline
# 0x xxx xxxx with separators
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}(?:[ .()-]*\d){7,13}(?!\d)")
_PHONE_LABELLED = re.compile(
    rf"\b{_PHONE_LABEL}\b[ \t]*(?:no\.?|number|#)?[ \t]*[:=\-–|#.]*[ \t]*(\+?\d[\d ().-]{{5,18}}\d)", re.IGNORECASE
)
_PHONE_UAE = re.compile(r"(?<![\w.+-])(?:05\d[ -]?\d{3}[ -]?\d{4}|0[2-9][ -]\d{3}[ -]?\d{4})(?![\w-])")
_PASSPORT = re.compile(r"\bpassport(?:\s*(?:no\.?|number|#))?\b[\s:=\-–|#.]*([A-Z0-9]{6,9})\b", re.IGNORECASE)

# Payment-processor ids (Stripe: pi_..., ch_..., cus_..., pm_..., evt_...) and keys (sk_live_..., whsec_...). The id part must
# hold a digit, so ordinary words such as "in_progress" are left alone.
_STRIPE = re.compile(
    r"\b(?:(?:pi|ch|cus|pm|src|tok|re|evt|in|sub|py|po|txn|card|acct|price|seti|si|cs|plink|ii|il|rcpt|ba|fee|dp|prod)_"
    r"(?=[A-Za-z0-9]*\d)[A-Za-z0-9]{8,}|(?:sk|pk|rk|whsec)_(?:live_|test_)?[A-Za-z0-9]{10,})\b"
)
# "Payment Reference: rxJduo1XlU57cgw3RuHDrWTz0", "Payment References: a + b + c", "Transaction ID: ...", "Auth code: ..."
_PAYMENT_LABEL = (
    r"(?:payment\s+(?:id|ids|ref(?:erence)?s?|intent|token)|transaction\s+(?:id|ref(?:erence)?|number|no\.?)|txn\s*(?:id|ref)?"
    r"|charge\s+id|receipt\s+(?:no\.?|number)|auth(?:ori[sz]ation)?\s+code|approval\s+code|customer\s+id|subscription\s+id"
    r"|invoice\s+ref(?:erence)?|payment\s+ref|رقم\s+العملية|رقم\s+المعاملة|مرجع\s+الدفع)"
)
_PAYMENT_REF = re.compile(
    rf"\b{_PAYMENT_LABEL}[ \t]*[:=\-–|#]*[ \t]*((?:[A-Za-z0-9_-]{{6,}})(?:[ \t]*[+,;/][ \t]*[A-Za-z0-9_-]{{6,}})*)", re.IGNORECASE
)
_TOKEN_PART = re.compile(r"[A-Za-z0-9_-]{6,}")
# credentials: a labelled password / key / token, and well-known secret shapes
_SECRET_LABELLED = re.compile(
    r"\b(?:password|passwd|pwd|passcode|pin(?:\s*code)?|api[ _-]?key|secret(?:[ _-]?key)?|access[ _-]?(?:key|token)|"
    r"auth[ _-]?token|private[ _-]?key|client[ _-]?secret|كلمة\s+(?:المرور|السر))\b[ \t]*[:=][ \t]*(\S{4,})",
    re.IGNORECASE,
)
_SECRET_SHAPES = re.compile(
    r"\b(?:AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}|ghp_[A-Za-z0-9]{30,}|xox[abp]-[A-Za-z0-9-]{10,})\b"
    r"|Bearer[ \t]+[A-Za-z0-9._~+/-]{20,}=*"
)
# tax, trade-licence and registration numbers (UAE TRN is 15 digits)
_TAX_LABEL = (
    r"(?:trn|tin|tax\s+registration\s+(?:no\.?|number)|tax\s+(?:id|number)|vat\s+(?:reg(?:istration)?\s+)?(?:no\.?|number)"
    r"|(?:trade\s+|commercial\s+)?licen[cs]e\s+(?:no\.?|number)|commercial\s+registration(?:\s+(?:no\.?|number))?|cr\s*(?:no\.?|number)"
    r"|الرقم\s+الضريبي|رقم\s+الرخصة|رقم\s+التسجيل\s+الضريبي|السجل\s+التجاري)"
)
_TAX_ID = re.compile(rf"\b{_TAX_LABEL}[ \t]*[:=\-–|#.]*[ \t]*([A-Z0-9](?:[A-Z0-9/-]| (?=\d)){{5,22}}[A-Z0-9])", re.IGNORECASE)
# any run of 10 or more digits (an account, card, phone or id that nothing labels); not part of a decimal, a
# grouped amount or a word
_LONG_NUMBER = re.compile(r"(?<![\w.,-])\d{10,}(?![\w-]|[.,]\d)")


def _custom_rules() -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    """Labels and patterns added in .env (PRIVACY_EXTRA_LABELS, PRIVACY_EXTRA_PATTERNS), so what counts as sensitive in a
    given company can change without a code change. Compiled once per distinct setting."""
    from rag.config.settings import get_settings

    s = get_settings()
    return _compile_custom(s.privacy_extra_labels, s.privacy_extra_patterns)


@functools.lru_cache(maxsize=8)
def _compile_custom(labels: str, patterns: str) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    label_rules = tuple(
        re.compile(
            rf"\b{re.escape(label.strip())}\b[ \t]*[:=\-–|#]*[ \t]*([^|\n;]{{3,80}}?)(?=[ \t]*(?:\||;|\n|$))", re.IGNORECASE
        )
        for label in labels.split(",")
        if label.strip()
    )
    pattern_rules = []
    for raw in patterns.split(";;"):
        if raw.strip():
            try:
                pattern_rules.append(re.compile(raw.strip()))
            except re.error:
                log.warning("PRIVACY_EXTRA_PATTERNS: ignoring invalid pattern %r", raw.strip())
    return label_rules, tuple(pattern_rules)


@dataclass(frozen=True)
class Finding:
    kind: str
    start: int
    end: int


def _iban_ok(value: str) -> bool:
    compact = re.sub(r"\s+", "", value).upper()
    if not 15 <= len(compact) <= 34:
        return False
    digits = "".join(str(int(ch, 36)) for ch in compact[4:] + compact[:4])
    return int(digits) % 97 == 1


def _luhn_ok(value: str) -> bool:
    digits = [int(ch) for ch in value if ch.isdigit()]
    if not 13 <= len(digits) <= 19 or len(set(digits)) == 1:
        return False
    total = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2:
            digit *= 2
            digit -= 9 if digit > 9 else 0
        total += digit
    return total % 10 == 0


def find(text: str, kinds: Sequence[str] = ALL_KINDS) -> list[Finding]:
    """The sensitive values in `text`, in order, without overlaps."""
    if not text:
        return []
    wanted = set(kinds)
    found: list[Finding] = []
    if "iban" in wanted:
        found += [Finding("iban", m.start(), m.end()) for m in _IBAN.finditer(text) if _iban_ok(m.group(0))]
    if "card" in wanted:
        found += [Finding("card", m.start(), m.end()) for m in _CARD.finditer(text) if _luhn_ok(m.group(0))]
    if "account" in wanted:
        for m in _ACCOUNT.finditer(text):
            if sum(ch.isdigit() for ch in m.group(1)) >= 6:
                found.append(Finding("account", m.start(1), m.end(1)))
    if "swift" in wanted:
        found += [Finding("swift", m.start(1), m.end(1)) for m in _SWIFT.finditer(text)]
    if "ssn" in wanted:
        found += [Finding("ssn", m.start(), m.end()) for m in _SSN.finditer(text)]
    if "emirates_id" in wanted:
        found += [Finding("emirates_id", m.start(), m.end()) for m in _EMIRATES_ID.finditer(text)]
    if "passport" in wanted:
        found += [
            Finding("passport", m.start(1), m.end(1)) for m in _PASSPORT.finditer(text) if any(ch.isdigit() for ch in m.group(1))
        ]
    if "email" in wanted:
        found += [Finding("email", m.start(), m.end()) for m in _EMAIL.finditer(text)]
    if "phone" in wanted:
        found += [
            Finding("phone", m.start(), m.end())
            for m in _PHONE_INTL.finditer(text)
            if 9 <= sum(ch.isdigit() for ch in m.group(0)) <= 15
        ]
        found += [
            Finding("phone", m.start(1), m.end(1))
            for m in _PHONE_LABELLED.finditer(text)
            if 7 <= sum(ch.isdigit() for ch in m.group(1)) <= 15
        ]
        found += [Finding("phone", m.start(), m.end()) for m in _PHONE_UAE.finditer(text)]
    if "payment_ref" in wanted:
        found += [Finding("payment_ref", m.start(), m.end()) for m in _STRIPE.finditer(text)]
        for m in _PAYMENT_REF.finditer(text):
            for part in _TOKEN_PART.finditer(m.group(1)):
                if any(ch.isdigit() for ch in part.group(0)):  # "Stripe Card Payments" is a method, not a reference
                    found.append(Finding("payment_ref", m.start(1) + part.start(), m.start(1) + part.end()))
    if "secret" in wanted:
        found += [Finding("secret", m.start(1), m.end(1)) for m in _SECRET_LABELLED.finditer(text)]
        found += [Finding("secret", m.start(), m.end()) for m in _SECRET_SHAPES.finditer(text)]
    if "tax_id" in wanted:
        for m in _TAX_ID.finditer(text):
            if sum(ch.isdigit() for ch in m.group(1)) >= 6:
                found.append(Finding("tax_id", m.start(1), m.end(1)))
    if "custom" in wanted:
        labels, patterns = _custom_rules()
        found += [Finding("custom", m.start(1), m.end(1)) for rule in labels for m in rule.finditer(text)]
        found += [Finding("custom", m.start(), m.end()) for rule in patterns for m in rule.finditer(text)]
    if "long_number" in wanted:  # last, so a number that is also a phone, card or account keeps its more specific kind
        found += [Finding("long_number", m.start(), m.end()) for m in _LONG_NUMBER.finditer(text)]
    # at the same place, a value its label names (tax id, account ...) is reported as that rather than as a bare card or number
    found.sort(key=lambda f: (f.start, -(f.end - f.start), {"card": 1, "long_number": 2}.get(f.kind, 0)))
    out: list[Finding] = []
    for f in found:
        if not out or f.start >= out[-1].end:
            out.append(f)
    return out


def has_sensitive(text: str, kinds: Sequence[str] = ALL_KINDS) -> bool:
    return bool(find(text, kinds))


def redact(text: str, kinds: Sequence[str] = ALL_KINDS) -> str:
    """Permanently replace each value with `[redacted kind]` (for stored text)."""
    out, last = [], 0
    for f in find(text, kinds):
        out.append(text[last : f.start])
        out.append(f"[redacted {f.kind.replace('_', ' ')}]")
        last = f.end
    out.append(text[last:])
    return "".join(out)


_PLACEHOLDER = re.compile(r"\[\[([A-Z_]+_\d+)\]\]")
_PLACEHOLDER_START = re.compile(r"\[(?:\[[A-Z_]*\d*\]?)?$")


class Vault:
    """Placeholders for the values of one request. The same value always gets the same placeholder within it."""

    def __init__(self, kinds: Sequence[str] = ALL_KINDS) -> None:
        self.kinds = tuple(kinds)
        self._by_value: dict[str, str] = {}
        self._by_token: dict[str, str] = {}
        self._counts: dict[str, int] = {}

    def __len__(self) -> int:
        return len(self._by_token)

    def learn(self, text: str) -> None:
        """Register the sensitive values of `text` without changing it."""
        for f in find(text, self.kinds):
            self._token(text[f.start : f.end], f.kind)

    def _token(self, value: str, kind: str) -> str:
        token = self._by_value.get(value)
        if token is None:
            n = self._counts[kind] = self._counts.get(kind, 0) + 1
            token = f"[[{kind.upper()}_{n}]]"
            self._by_value[value], self._by_token[token[2:-2]] = token, value
        return token

    def mask(self, text: str) -> str:
        out, last = [], 0
        for f in find(text, self.kinds):
            out.append(text[last : f.start])
            out.append(self._token(text[f.start : f.end], f.kind))
            last = f.end
        out.append(text[last:])
        masked = "".join(out)
        # a value found anywhere in this request is masked everywhere in it, also where nothing labels it ("search 0123456789")
        for value in sorted(self._by_value, key=len, reverse=True):
            if value in masked:
                masked = re.sub(rf"(?<!\w){re.escape(value)}(?!\w)", lambda _m, t=self._by_value[value]: t, masked)
        return masked

    def unmask(self, text: str) -> str:
        if not self._by_token or "[[" not in text:
            return text
        return _PLACEHOLDER.sub(lambda m: self._by_token.get(m.group(1), m.group(0)), text)

    # ---- structures -------------------------------------------------------------------------------------------------------
    def _walk(self, obj: Any, fn: Callable[[str], str]) -> Any:
        if isinstance(obj, str):
            return fn(obj)
        if isinstance(obj, dict):
            return {k: self._walk(v, fn) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._walk(v, fn) for v in obj]
        return obj

    def mask_messages(self, messages: Sequence[ChatMessage]) -> list[ChatMessage]:
        for message in messages:  # learn every value first, so an earlier message is masked for a value found in a later one
            if isinstance(message.content, str):
                self.learn(message.content)
            else:
                for part in message.content:
                    if part.get("type") == "text" and isinstance(part.get("text"), str):
                        self.learn(part["text"])
        out = []
        for message in messages:
            content = message.content
            if isinstance(content, str):
                content = self.mask(content)
            else:
                content = [
                    dict(p, text=self.mask(p["text"])) if p.get("type") == "text" and isinstance(p.get("text"), str) else p
                    for p in content
                ]
            calls = [ToolCall(c.id, c.name, self._walk(c.arguments, self.mask)) for c in message.tool_calls]
            out.append(replace(message, content=content, tool_calls=calls))
        return out

    def unmask_result(self, result: ChatResult) -> ChatResult:
        calls = [ToolCall(c.id, c.name, self._walk(c.arguments, self.unmask)) for c in result.tool_calls]
        return replace(result, content=self.unmask(result.content or ""), tool_calls=calls)


def _has_image(messages: Sequence[ChatMessage]) -> bool:
    return any(not isinstance(m.content, str) and any(p.get("type") == "image" for p in m.content) for m in messages)


class ShieldedChat:
    """A chat model whose requests are masked on the way out and whose replies are unmasked on the way back."""

    def __init__(self, inner: Any, kinds: Sequence[str] = ALL_KINDS) -> None:
        self._inner = inner
        self._kinds = tuple(kinds)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @property
    def inner(self) -> Any:
        return self._inner

    async def aclose(self) -> None:
        close = getattr(self._inner, "aclose", None)
        if close is not None:
            await close()

    async def complete(self, messages: Sequence[ChatMessage], **kwargs: Any) -> ChatResult:
        if _has_image(messages):
            return await self._inner.complete(messages, **kwargs)  # pixels cannot be masked: callers keep such pages away
        vault = Vault(self._kinds)
        masked = vault.mask_messages(messages)
        token = MASKED_VALUES.set(len(vault))
        try:
            result = await self._inner.complete(masked, **kwargs)
        finally:
            MASKED_VALUES.reset(token)
        return vault.unmask_result(result)

    async def stream(self, messages: Sequence[ChatMessage], **kwargs: Any) -> AsyncIterator[StreamChunk]:
        if _has_image(messages):
            async for chunk in self._inner.stream(messages, **kwargs):
                yield chunk
            return
        vault = Vault(self._kinds)
        masked = vault.mask_messages(messages)
        pending = ""
        token = MASKED_VALUES.set(len(vault))  # read by the metering layer when the call starts
        try:
            async for chunk in self._inner.stream(masked, **kwargs):
                if chunk.result is not None:
                    if pending:
                        yield StreamChunk(delta=vault.unmask(pending))
                        pending = ""
                    yield StreamChunk(delta="", result=vault.unmask_result(chunk.result))
                    continue
                if not len(vault):
                    yield chunk
                    continue
                pending += chunk.delta
                # hold back what could be the start of a placeholder ("[", "[[", "[[ACCOUNT_1",
                # "[[ACCOUNT_1]") until it is complete
                tail = _PLACEHOLDER_START.search(pending)
                if tail is not None:
                    emit, pending = pending[: tail.start()], pending[tail.start() :]
                else:
                    emit, pending = pending, ""
                if emit:
                    yield StreamChunk(delta=vault.unmask(emit))
            if pending:
                yield StreamChunk(delta=vault.unmask(pending))
        finally:
            try:
                MASKED_VALUES.reset(token)
            except ValueError:  # the generator was closed from another context: the value dies with that context anyway
                pass
