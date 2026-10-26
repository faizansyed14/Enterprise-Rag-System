"""Protected files are recognised before they are queued: password PDFs, rights-protected PDFs, encrypted Office files."""
import asyncio
import io
import zipfile
from types import SimpleNamespace

from rag.api.routers import uploads
from rag.ingestion import protection


def _pdf(user_password: str = "", owner_password: str = "") -> bytes:
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas

    buffer = io.BytesIO()
    kwargs = {}
    if user_password or owner_password:
        kwargs["encrypt"] = StandardEncryption(user_password, ownerPassword=owner_password or "owner-secret", canPrint=0)
    page = canvas.Canvas(buffer, pagesize=A4, **kwargs)
    page.drawString(72, 700, "Licence No. 123 expires 2027")
    page.save()
    return buffer.getvalue()


def _docx(password: str | None = None) -> bytes:
    from docx import Document

    plain = io.BytesIO()
    doc = Document()
    doc.add_paragraph("Lease for unit 108")
    doc.save(plain)
    if not password:
        return plain.getvalue()
    import msoffcrypto

    plain.seek(0)
    out = io.BytesIO()
    file = msoffcrypto.OfficeFile(plain)
    file.encrypt(password, out)
    return out.getvalue()


def _mark_encrypted(zip_bytes: bytes, member: bytes) -> bytes:
    """Set the 'encrypted' flag of one member in the ZIP's central directory (zipfile cannot write encrypted members)."""
    data = bytearray(zip_bytes)
    at = data.index(b"PK")
    while at >= 0:
        name_len = int.from_bytes(data[at + 28 : at + 30], "little")
        if bytes(data[at + 46 : at + 46 + name_len]) == member:
            data[at + 8] |= 0x01
            return bytes(data)
        at = data.find(b"PK", at + 4)
    raise AssertionError("member not found")


def test_a_normal_pdf_and_a_restricted_only_pdf_are_readable():
    assert protection.detect(_pdf(), "a.pdf") is None
    # an owner password with no user password: it opens without asking, so it is not reported
    assert protection.detect(_pdf(user_password="", owner_password="owner-secret"), "restricted.pdf") is None


def test_a_pdf_that_needs_a_password_is_reported():
    assert protection.detect(_pdf(user_password="secret"), "locked.pdf") == protection.PDF_PASSWORD


def test_a_microsoft_rights_protected_pdf_is_reported():
    data = b"%PDF-1.7\n1 0 obj << /Filter /MicrosoftIRMServices /V 4 >> endobj\ntrailer << /Encrypt 1 0 R >>\n%%EOF"
    assert protection.detect(data, "Badr License.pdf") == protection.PDF_RIGHTS


def test_an_encrypted_office_file_is_reported_and_a_plain_one_is_not(tmp_path):
    assert protection.detect(_docx(), "lease.docx") is None
    locked = _docx("secret")
    assert locked[:4] == b"\xd0\xcf\x11\xe0"  # an encrypted .docx is not a ZIP any more
    assert protection.detect(locked, "lease.docx") == protection.OFFICE_PASSWORD
    path = tmp_path / "x.docx"
    path.write_bytes(locked)
    assert protection.detect(path, "x.docx") == protection.OFFICE_PASSWORD  # also from a file on disk


def test_other_files_and_odd_input_never_raise():
    assert protection.detect(b"", "empty.pdf") is None
    assert protection.detect(b"just some text", "notes.txt") is None
    assert protection.detect(b"%PDF-1.4 /Encrypt but cut off", "broken.pdf") is None  # cannot tell: left to the reader


def test_zip_members_are_checked_when_the_zip_is_opened(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("Docs/open.pdf", _pdf())
        zf.writestr("Docs/locked.pdf", _pdf(user_password="secret"))
        zf.writestr("Docs/lease.docx", _docx("secret"))
        zf.writestr("Docs/plain.docx", _docx())
        zf.writestr("Docs/secret.txt", b"hidden")
    data = _mark_encrypted(buffer.getvalue(), b"Docs/secret.txt")
    settings = SimpleNamespace(upload_staging_dir=str(tmp_path), cache_dir=str(tmp_path), upload_max_mb=5, upload_zip_max_mb=10,
                               skip_extension_set=frozenset())
    out = asyncio.run(uploads.extract_zip(SimpleNamespace(id="u"), SimpleNamespace(settings=settings),
                                          SimpleNamespace(file=io.BytesIO(data), filename="Pack.zip")))
    files = {f["path"]: f for f in out["files"]}
    assert files["Docs/open.pdf"]["protected"] is None and files["Docs/open.pdf"]["selected"]
    assert files["Docs/plain.docx"]["protected"] is None and files["Docs/plain.docx"]["selected"]
    assert files["Docs/locked.pdf"]["protected"] == protection.PDF_PASSWORD and not files["Docs/locked.pdf"]["selected"]
    assert files["Docs/lease.docx"]["protected"] == protection.OFFICE_PASSWORD and not files["Docs/lease.docx"]["selected"]
    assert files["Docs/secret.txt"]["protected"] == protection.ZIP_PASSWORD and not files["Docs/secret.txt"]["selected"]


def test_a_single_protected_file_is_refused_before_anything_is_queued(monkeypatch):
    async def run():
        target = SimpleNamespace(settings=SimpleNamespace(upload_max_mb=5, clamav_url=""), services=None, user=SimpleNamespace(id="u"))
        return await uploads._register(target, "locked.pdf", "locked.pdf", _pdf(user_password="secret"), "application/pdf")

    result = asyncio.run(run())  # no services, so reaching storage or the queue would fail loudly
    assert result["ok"] is False and result["protected"] is True and result["error"] == protection.PDF_PASSWORD
