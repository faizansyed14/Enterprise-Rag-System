"""Controlled vocabularies: document types (ISO 19650 type codes plus common names), CSI MasterFormat
divisions. Each entry lists the words people use for it in English and Arabic, so folder names, file names, title
blocks and questions all map to the same value.
"""

from __future__ import annotations

import re

from rag.text.normalize import normalize_for_search

# ISO 19650 type codes and common names. Keys are the stored value.
DOC_TYPES: dict[str, dict[str, object]] = {
    "drawing": {"codes": ["DR", "DWG", "SK", "SHD"], "words": ["drawing", "drawings", "plan", "elevation", "section", "detail", "sketch", "shop drawing", "مخطط", "مخططات", "رسومات", "لوحة"]},
    "specification": {"codes": ["SP", "SPEC"], "words": ["specification", "specifications", "spec", "specs", "مواصفات", "المواصفات"]},
    "rfi": {"codes": ["RFI", "RI", "TQ"], "words": ["rfi", "rfis", "request for information", "technical query", "طلب معلومات", "استفسار"]},
    "submittal": {"codes": ["SUB", "SUBMITTAL", "MAR", "MTL"], "words": ["submittal", "submittals", "submission", "material approval", "اعتماد مواد", "تقديم"]},
    "minutes": {"codes": ["MI", "MOM"], "words": ["minutes", "minutes of meeting", "meeting minutes", "mom", "محضر", "محضر اجتماع", "محاضر"]},
    "report": {"codes": ["RP", "RPT"], "words": ["report", "reports", "progress report", "تقرير", "تقارير"]},
    "change_order": {"codes": ["CO", "PCO", "VO", "VOR", "CCD", "COR"], "words": ["change order", "variation", "variation order", "change request", "أمر تغيير", "أمر تغييري", "تغييرات"]},
    "invoice": {"codes": ["INV", "IPC"], "words": ["invoice", "invoices", "payment certificate", "interim payment", "فاتورة", "فواتير", "شهادة دفع"]},
    "contract": {"codes": ["CT", "CON", "AGR"], "words": ["contract", "agreement", "subcontract", "عقد", "اتفاقية"]},
    "correspondence": {"codes": ["CO_", "LTR", "COR_", "CORR"], "words": ["letter", "correspondence", "email", "memo", "مراسلة", "خطاب", "رسالة"]},
    "transmittal": {"codes": ["TRN", "TR", "TRANSMITTAL"], "words": ["transmittal", "transmittals", "إرسال", "ارسالية"]},
    "schedule": {"codes": ["SH", "PR", "SCH"], "words": ["schedule", "programme", "program", "baseline", "جدول زمني", "برنامج"]},
    "boq": {"codes": ["BQ", "BOQ"], "words": ["bill of quantities", "boq", "quantities", "جدول كميات", "كميات"]},
    "calculation": {"codes": ["CA", "CALC"], "words": ["calculation", "calculations", "design calculation", "حسابات"]},
    "method_statement": {"codes": ["MS"], "words": ["method statement", "طريقة تنفيذ"]},
    "site_instruction": {"codes": ["SI", "AI", "ASI", "CVI"], "words": ["site instruction", "architect's instruction", "engineer's instruction", "تعليمات الموقع", "تعليمات موقع"]},
    "inspection": {"codes": ["IR", "WIR", "MIR", "ITP", "NCR"], "words": ["inspection", "inspection request", "non-conformance", "ncr", "itp", "فحص", "طلب فحص", "عدم مطابقة"]},
    "model": {"codes": ["M2", "M3", "CM"], "words": ["model", "bim", "نموذج"]},
    "survey": {"codes": ["SU"], "words": ["survey", "مسح"]},
    "photo": {"codes": ["PH"], "words": ["photo", "photograph", "صورة"]},
}

MASTERFORMAT_DIVISIONS: dict[str, str] = {
    "00": "Procurement and Contracting Requirements", "01": "General Requirements", "02": "Existing Conditions",
    "03": "Concrete", "04": "Masonry", "05": "Metals", "06": "Wood, Plastics, and Composites",
    "07": "Thermal and Moisture Protection", "08": "Openings", "09": "Finishes", "10": "Specialties",
    "11": "Equipment", "12": "Furnishings", "13": "Special Construction", "14": "Conveying Equipment",
    "21": "Fire Suppression", "22": "Plumbing", "23": "Heating, Ventilating, and Air Conditioning (HVAC)",
    "25": "Integrated Automation", "26": "Electrical", "27": "Communications", "28": "Electronic Safety and Security",
    "31": "Earthwork", "32": "Exterior Improvements", "33": "Utilities", "34": "Transportation",
    "35": "Waterway and Marine Construction", "40": "Process Interconnections",
    "41": "Material Processing and Handling Equipment", "42": "Process Heating, Cooling, and Drying Equipment",
    "43": "Process Gas and Liquid Handling, Purification, and Storage Equipment",
    "44": "Pollution and Waste Control Equipment", "45": "Industry-Specific Manufacturing Equipment",
    "46": "Water and Wastewater Equipment", "48": "Electrical Power Generation",
}

# MasterFormat divisions accepted as a specification section when a file name starts with one ("03 30 00 Concrete").
SPEC_DIVISIONS = frozenset({"03", "04", "05", "06", "07", "08", "09", "10", "21", "22", "23", "25", "26", "27", "28", "31", "32", "33"})



def _pattern(words: list[str]) -> re.Pattern[str]:
    parts = sorted({re.escape(normalize_for_search(w)) for w in words}, key=len, reverse=True)
    return re.compile(r"(?<![\w])(?:" + "|".join(parts) + r")(?![\w])")


_DOCTYPE_PATTERNS = {k: _pattern(v["words"]) for k, v in DOC_TYPES.items()}  # type: ignore[arg-type]


def doc_type_from_text(text: str) -> str | None:
    folded = normalize_for_search(text)
    best: tuple[int, str] | None = None
    for key, pattern in _DOCTYPE_PATTERNS.items():
        m = pattern.search(folded)
        if m and (best is None or m.start() < best[0]):
            best = (m.start(), key)
    return best[1] if best else None


def doc_type_from_code(code: str) -> str | None:
    code = code.upper().strip()
    for key, entry in DOC_TYPES.items():
        if code in entry["codes"]:  # type: ignore[operator]
            return key
    return None


def division_for_spec(spec_key: str) -> tuple[str, str] | None:
    digits = spec_key.removeprefix("SPEC-")[:2]
    title = MASTERFORMAT_DIVISIONS.get(digits)
    return (digits, title) if title else None
