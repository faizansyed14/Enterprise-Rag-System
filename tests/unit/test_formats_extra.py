from __future__ import annotations

import asyncio
import io
import zipfile

from rag.ingestion.formats import detect_format, parse_document
from rag.ingestion.formats_extra import read_odt, read_rtf, read_xer

XER = ("ERMHDR\t19.12\t2026-09-14\n%T\tTASK\n%F\ttask_id\ttask_code\ttask_name\tstatus_code\tphys_complete_pct\ttotal_float_hr_cnt\twbs_id\n"
       "%R\t1\tC1000\tMobilisation & site setup\tTK_Complete\t100\t0\t9\n%R\t2\tC1100\tBlock A substructure\tTK_Active\t60\t776\t9\n%E\n"
       "%T\tTASKPRED\n%F\ttask_id\tpred_task_id\tpred_type\tlag_hr_cnt\n%R\t2\t1\tPR_FS\t0\n%E\n%T\tUDFVALUE\n%F\tx\n%R\t1\n%E\n").encode()


def test_xer_schedule_is_read_with_activity_codes_in_the_links() -> None:
    assert detect_format(XER, "plan.xer") == "xer"
    doc = read_xer(XER)
    text = " ".join(" ".join(c for r in t.rows for c in r) for p in doc.pages for t in p.tables)
    assert "C1100" in text and "Block A substructure" in text
    pred = [t for p in doc.pages for t in p.tables if t.rows[0][0] == "predecessor"][0]
    assert pred.rows[1][:2] == ["C1000", "C1100"]  # ids replaced by activity codes
    assert "UDFVALUE" not in text and "wbs_id" not in text  # internal tables and key columns are left out


def test_rtf_text_and_unicode_are_extracted_and_destinations_skipped() -> None:
    rtf = rb"{\rtf1\ansi{\fonttbl{\f0 Arial;}}\pard Hello \b world\b0.\par Second paragraph with caf\'e9 and \u1575?\par}"
    doc = read_rtf(rtf)
    text = doc.pages[0].text
    assert "Hello world." in text and "Second paragraph with café" in text and "Arial" not in text


def test_odt_headings_paragraphs_and_tables() -> None:
    content = (b'<?xml version="1.0"?><office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
               b'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0">'
               b'<office:body><office:text><text:h text:outline-level="1">Scope</text:h><text:p>The works include piling.</text:p>'
               b'<table:table><table:table-row><table:table-cell><text:p>Item</text:p></table:table-cell><table:table-cell><text:p>Qty</text:p></table:table-cell></table:table-row>'
               b'<table:table-row><table:table-cell><text:p>Piles</text:p></table:table-cell><table:table-cell><text:p>40</text:p></table:table-cell></table:table-row></table:table>'
               b'</office:text></office:body></office:document-content>')
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", content)
    assert detect_format(buffer.getvalue(), "spec.odt") == "odt"
    doc = asyncio.run(parse_document(buffer.getvalue(), "spec.odt", pdf_parser=None, ocr=None))
    page = doc.pages[0]
    assert "# Scope" in page.text and "The works include piling." in page.text
    assert page.tables[0].rows == [["Item", "Qty"], ["Piles", "40"]]


def test_hostile_xml_is_refused() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        zf.writestr("content.xml", b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>')
    try:
        read_odt(buffer.getvalue())
    except Exception as exc:  # noqa: BLE001
        assert "DOCTYPE" in str(exc)
    else:
        raise AssertionError("a DOCTYPE with entities must be refused")


def test_code_and_log_files_are_text() -> None:
    assert detect_format(b"def f():\n    return 1\n", "tool.py") == "text"
    assert detect_format(b"2026-09-14 INFO started\n", "site.log") == "text"
    assert detect_format(b"SELECT 1;", "q.sql") == "text"
