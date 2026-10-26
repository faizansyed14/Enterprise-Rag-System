"""Arithmetic in answers is done by the program, not trusted to the model.

Language models often write "the total is 2240.7 + 2287.95" and stop, or add wrongly. After the answer is written:

  * every sum left unworked ("a + b + c" with no "=" after it) is worked out exactly and added below the answer, and
  * a number that is the sum or difference of numbers in the same sentence counts as supported when citations are checked
    (it was calculated from the document's numbers, so it is not expected to appear in the document itself).
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from itertools import combinations

_NUM = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
# two or more numbers joined by " + ", not followed by "=" (an unfinished total); spaces around "+" keep dates and phone
# numbers ("+971 50 ...", "2025-01-03") out
_SUM = re.compile(rf"(?<![\w.,])((?:{_NUM})(?:\s+\+\s+(?:{_NUM}))+)(?![\d.,])(?!\s*[=+])")


def _value(text: str) -> Decimal | None:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _format(value: Decimal, like: list[str]) -> str:
    places = max((len(t.split(".")[1]) for t in like if "." in t), default=0)
    quantised = value.quantize(Decimal(1).scaleb(-places)) if places else value.quantize(Decimal(1))
    if any("," in t for t in like) or abs(quantised) >= 10_000:
        return f"{quantised:,.{places}f}"
    return f"{quantised:.{places}f}"


def unworked_sums(answer: str) -> list[str]:
    """'2240.7 + 2287.95 = 4528.65' for every sum the answer leaves without a result."""
    out: list[str] = []
    for match in _SUM.finditer(answer):
        parts = re.findall(_NUM, match.group(1))
        values = [_value(p) for p in parts]
        if len(values) < 2 or any(v is None for v in values):
            continue
        line = f"{' + '.join(parts)} = {_format(sum(values), parts)}"  # type: ignore[arg-type]
        if line not in out:
            out.append(line)
    return out


def derived(number: str, sentence_numbers: list[str]) -> bool:
    """True when `number` is the sum of all, or the sum or difference of two, of the other numbers in the sentence."""
    target = _value(number)
    if target is None:
        return False
    others = [v for v in (_value(n) for n in sentence_numbers if n != number) if v is not None]
    if len(others) < 2:
        return False
    tolerance = Decimal("0.011")
    if abs(sum(others) - target) <= tolerance:
        return True
    return any(abs((a + b) - target) <= tolerance or abs(abs(a - b) - target) <= tolerance for a, b in combinations(others, 2))
