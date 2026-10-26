"""Is this file protected, so that its text cannot be read without a password or an account that owns the rights?

Checked cheaply and before anything is queued, so the upload dialog can say so ("Protected: password needed") instead of the
file failing minutes later in a worker. A file is reported only when it really cannot be read:

  PDF         needs a password to open (a PDF with only an owner password and restrictions opens fine, and is not reported),
              or is wrapped by Microsoft rights management (Microsoft Information Protection / IRM)
  Office      .docx .xlsx .pptx (and the old .doc .xls .ppt) saved with a password, or with rights management: those are
              encrypted containers, not ZIPs
  ZIP         members that are password-protected (see ingestion/archive.py)

`detect` never raises: when it cannot tell, the file is treated as readable and the reader reports whatever is wrong.
"""

from __future__ import annotations

import io
import logging
from pathlib import Path

from rag.ingestion.formats import extension

log = logging.getLogger(__name__)

PREFIX = "Protected: "
PDF_PASSWORD = PREFIX + "this PDF needs a password to open."
PDF_RIGHTS = PREFIX + "this PDF is rights-protected (Microsoft Information Protection); only authorised accounts can open it."
OFFICE_PASSWORD = PREFIX + "this Office file is encrypted (password or rights management); it cannot be read without it."
ZIP_PASSWORD = PREFIX + "password-protected inside the ZIP."

_OLE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_OFFICE = {"docx", "docm", "xlsx", "xlsm", "pptx", "pptm", "doc", "xls", "ppt", "dotx", "xltx", "potx"}
_HEAD, _TAIL = 64 * 1024, 512 * 1024  # where a PDF's trailer and encryption dictionary are looked for


def detect(source: bytes | Path, name: str) -> str | None:
    """The reason a file cannot be read because it is protected, or None."""
    try:
        ext = extension(name)
        head, tail = _ends(source)
        if head.startswith(b"%PDF") or b"%PDF-" in head[:1024]:
            return _pdf(source, head, tail)
        if head.startswith(_OLE) and ext in _OFFICE:
            return _office(source) or (OFFICE_PASSWORD if ext.endswith(("x", "m")) else None)
    except Exception:  # noqa: BLE001 - a detector must never stop an upload
        log.debug("protection check failed for %s", name, exc_info=True)
    return None


def _ends(source: bytes | Path) -> tuple[bytes, bytes]:
    """The beginning of the file and the end of it (a PDF's trailer sits at the end); the whole file when it is small."""
    if isinstance(source, bytes):
        return source[:_HEAD], source[-_TAIL:]
    size = source.stat().st_size
    with source.open("rb") as f:
        if size <= _HEAD + _TAIL:
            data = f.read()
            return data[:_HEAD], data
        head = f.read(_HEAD)
        f.seek(size - _TAIL)
        return head, f.read()


def _pdf(source: bytes | Path, head: bytes, tail: bytes) -> str | None:
    window = head + tail
    if b"MicrosoftIRMServices" in window:
        return PDF_RIGHTS  # /Encrypt << /Filter /MicrosoftIRMServices >>
    if b"/Encrypt" not in window:
        return None
    import pypdfium2 as pdfium
    from pypdfium2._helpers.misc import PdfiumError

    try:
        doc = pdfium.PdfDocument(source if isinstance(source, bytes) else str(source))
    except PdfiumError as exc:
        return PDF_PASSWORD if "password" in str(exc).lower() else None
    doc.close()
    return None  # encrypted with an empty user password (restrictions only): readable


def _office(source: bytes | Path) -> str | None:
    import msoffcrypto

    handle = io.BytesIO(source) if isinstance(source, bytes) else source.open("rb")
    try:
        return OFFICE_PASSWORD if msoffcrypto.OfficeFile(handle).is_encrypted() else None
    except Exception:  # noqa: BLE001 - an OLE file msoffcrypto does not know (the caller still judges by the extension)
        return None
    finally:
        handle.close()
