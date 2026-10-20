from __future__ import annotations

import pytest

from rag.ingestion.chunker import Chunker, HeaderFields, PageForChunking, chunk_id
from rag.metadata.extract import MetadataExtractor, PageInput
from rag.metadata.file_naming import parse_filename
from rag.metadata.identifiers import extract_identifiers, identifier_key
from rag.metadata.path_rules import PathRules
from rag.metadata.references import find_references
from rag.metadata.title_block import parse_title_block
from rag.text.bm25 import BM25Analyzer
from rag.text.language import detect_language, languages_compatible
from rag.text.minhash import band_keys, signature, similarity
from rag.text.normalize import clean_text, normalize_for_search
from rag.text.ocr_spacing import restore_spaces
from rag.text.tokens import TokenCounter


@pytest.mark.parametrize(("a", "b"), [
    ("A-101", "a101"), ("RFI #023", "RFI-23"), ("RFI No. 23", "rfi 023"), ("03 30 00", "033000"),
    ("SUB-084", "sub 84"), ("PRJ1-ABC-ZZ-01-DR-A-1001", "prj1-abc-zz-01-dr-a-1001"),
])
def test_identifier_spellings_share_one_key(a: str, b: str) -> None:
    assert identifier_key(a) == identifier_key(b)


def test_identifier_extraction_skips_years_and_dates() -> None:
    keys = [i.key for i in extract_identifiers("Meeting on 12/03/2024 in 2024 about RFI-023 and drawing A-101")]
    assert keys == ["RFI-23", "A-101"]


def test_iso19650_name_drops_status_and_revision_suffix() -> None:
    [ident] = extract_identifiers("PRJ1-ABC-ZZ-01-DR-A-1001-S2-P03 Ground floor plan")
    assert ident.raw == "PRJ1-ABC-ZZ-01-DR-A-1001"


@pytest.mark.parametrize(("name", "number", "doc_type"), [
    ("PRJ1-ABC-ZZ-01-DR-A-1001_S2_P03.pdf", "PRJ1-ABC-ZZ-01-DR-A-1001", "drawing"),
    ("A-101 Rev C - Ground Floor Plan.pdf", "A-101", "drawing"),
    ("RFI-023 Response from Engineer.pdf", "RFI-023", "rfi"),
    ("033000_Concrete_rev2.pdf", "03 30 00", "specification"),
    ("SUB-084_Rebar Shop Drawings_RevB.pdf", "SUB-084", "submittal"),
])
def test_file_names(name: str, number: str, doc_type: str) -> None:
    fields = parse_filename(name)
    assert (fields.doc_number, fields.doc_type) == (number, doc_type)


def test_title_block_values_on_same_and_next_lines() -> None:
    same = parse_title_block("DRAWING NO: A-101     REV: C\nDRAWING TITLE: GROUND FLOOR PLAN\nSCALE: 1:100")
    assert same == {"doc_number": "A-101", "title": "GROUND FLOOR PLAN", "scale": "1:100"}  # the REV value stops the number
    nxt = parse_title_block("DRAWING NO.\nS-201\nREVISION\nP02")
    assert nxt["doc_number"] == "S-201" and "revision" not in nxt


def test_path_rules_find_project_and_type() -> None:
    rules = PathRules(known_projects=["Marina Tower"])
    out = rules.apply("/Projects/Marina Tower/02_Structural/Drawings/S-201.pdf")
    assert out["project"][0] == "Marina Tower" and "discipline" not in out
    assert out["doc_type"][0] == "drawing"


def test_metadata_rules_prefer_reliable_sources() -> None:
    md = MetadataExtractor().extract(
        path="Marina Tower/RFIs/scan001.pdf", source_fields={"title": "Fire damper access"},
        pages=[PageInput(1, "REQUEST FOR INFORMATION No. 023\nProject: Marina Tower Date: 03/03/2025\n"
                            "Subject: Fire damper access")])
    assert md.get("doc_number") == "RFI-023" and md.fields["title"].source == "source"
    assert md.get("doc_type") == "rfi" and "revision" not in md.fields and "issue_date" not in md.fields


def test_references_and_their_kind() -> None:
    refs = find_references([(1, "This drawing follows A-100. Refer to Section 03 30 00 and RFI-023.")], {"A-101"})
    kinds = {(r.target, r.kind) for r in refs}
    assert ("A-100", "refers_to") in kinds and ("SPEC-033000", "refers_to") in kinds


def test_normalisation_for_arabic_and_digits() -> None:
    assert normalize_for_search("الخرسانةُ ٤٠") == normalize_for_search("الخرسانه 40")
    assert clean_text("a\x00b  c\n\n\n\nd") == "a b c\n\nd"


def test_language_detection() -> None:
    assert detect_language("The concrete for all columns shall be grade C40 as specified.") == "en"
    assert detect_language("يجب أن تكون الخرسانة من الفئة C40 للأعمدة في المستوى الثالث") == "ar"
    assert languages_compatible("ar", {"en"}) is False and languages_compatible("und", {"en"}) is True


def test_bm25_identifiers_and_stemming() -> None:
    analyzer = BM25Analyzer()
    tokens = analyzer.tokens("Placing concrete columns per drawing A-101")
    assert "#a-101" in tokens and "column" in tokens and "per" not in tokens
    assert "#a-101" in analyzer.tokens("what about a101?", query=True)
    vec = analyzer.document_vector("concrete concrete concrete")
    assert len(vec.indices) == 1 and vec.values[0] > 1.0


def test_minhash_finds_near_duplicates_only() -> None:
    base = " ".join(f"word{i} item{i % 7} spec{i % 11}" for i in range(300))
    copy = base.replace("word10 ", "changed ")
    other = " ".join(f"zeta{i} beta{i % 5}" for i in range(300))
    a, b, c = signature(base), signature(copy), signature(other)
    assert similarity(a, b) > 0.85 and similarity(a, c) < 0.1
    assert set(band_keys(a)) & set(band_keys(b))


def test_ocr_spacing_restores_words_but_not_codes() -> None:
    assert restore_spaces("Installfire dampersatall shaftpenetrationsonLevel 3.") == \
        "Install fire dampers at all shaft penetrations on Level 3."
    assert restore_spaces("SITE INSTRUCTION SI-014 grade C40 1.5 m3") == "SITE INSTRUCTION SI-014 grade C40 1.5 m3"
    assert restore_spaces("rescheduled to24March2026 by18March") == "rescheduled to 24 March 2026 by 18 March"
    assert restore_spaces("Chair:KarimHaddad") == "Chair: Karim Haddad"
    assert restore_spaces("Facademock-upinspection") == "Facade mock-up inspection"


def test_chunker_follows_structure_tables_and_pages() -> None:
    spec = ("SECTION 03 30 00\nPART 2 - PRODUCTS\n2.01 MIXES\nA. Columns use grade C40.\n"
            "PART 3 - EXECUTION\n3.01 PLACING\nA. Do not place above 35 degrees C.")
    pages = [PageForChunking(1, spec), PageForChunking(2, "Mix schedule\n[[TABLE:0]]", tables=[
        {"rows": [["Element", "Grade"], ["Columns", "C40"], ["Slabs", "C35"]], "header_rows": 1}])]
    chunks = Chunker(TokenCounter(), chunk_tokens=40, min_tokens=0).chunk(
        pages, HeaderFields(project="MT", doc_number="03 30 00", title="Concrete",
                            doc_number_key="SPEC-033000"), version_id="v1")
    assert [c.page_start for c in chunks] == [1, 1, 2]
    assert chunks[0].section_path.endswith("2.01 MIXES") and chunks[1].section_path.endswith("3.01 PLACING")
    assert chunks[2].kind == "table" and "Element: Columns | Grade: C40" in chunks[2].text
    assert all(c.header.startswith("MT | 03 30 00 | Concrete") for c in chunks)
    assert all("SPEC-033000" in c.identifiers for c in chunks)
    assert chunks[0].id == chunk_id("v1", 0)


def test_chunker_reads_markdown_and_html_tables_from_parsers() -> None:
    page = (
        "# **CAPE HAYAT**\n## TOWER 3\n\n## LEVEL 01\n\n"
        "| Unit | Type |\n| ---- | ---------------- |\n| 01 | 1 BEDROOM TYPE 1 |\n| 02 | STUDIO |\n\n"
        "## PENTHOUSE\n\n"
        "| TYPE 2<br/>APARTMENT AREA<br/>TOTAL AREA | SQ.M<br/>474.57<br/>633.57 | SQ.F<br/>5107.04<br/>6819.5 |\n"
        "| --- | --- | --- |\n\n"
        "<table><thead><tr><th>ROOM</th><th>Dimensions</th></tr></thead>"
        "<tbody><tr><td>BALCONY</td><td>8000 x 3100</td></tr></tbody></table>"
    )
    chunks = Chunker(TokenCounter(), chunk_tokens=200).chunk([PageForChunking(1, page)], HeaderFields(), version_id="v1")
    text = "\n".join(c.text for c in chunks)
    assert "Unit: 02 | Type: STUDIO" in text and "Unit: 01 | Type: 1 BEDROOM TYPE 1" in text
    assert "TYPE 2: APARTMENT AREA | SQ.M: 474.57 | SQ.F: 5107.04" in text
    assert "ROOM: BALCONY | Dimensions: 8000 x 3100" in text
    assert chunks[0].section_path.startswith("CAPE HAYAT") and "**" not in "".join(c.section_path for c in chunks)
    assert all(c.kind == "table" for c in chunks if "STUDIO" in c.text)


def test_consecutive_headings_stay_together_so_a_table_keeps_its_tower() -> None:
    page = ("# CAPE HAYAT\n## TOWER 3\n\n## LEVEL 01\n\n| Unit | Type |\n| --- | --- |\n| 01 | STUDIO |\n| 02 | STUDIO |\n\n"
            "## LEVELS 02-03\n\n| Unit | Type |\n| --- | --- |\n| 03 | 1 BEDROOM TYPE 3 |\n| 04 | STUDIO |")
    chunks = Chunker(TokenCounter(), chunk_tokens=200).chunk([PageForChunking(1, page)], HeaderFields(), version_id="v1")
    assert [c.section_path for c in chunks] == ["CAPE HAYAT > TOWER 3 > LEVEL 01", "CAPE HAYAT > TOWER 3 > LEVELS 02-03"]


def test_tiny_neighbours_are_merged_but_tables_and_far_sections_are_not() -> None:
    page = ("# Scope\n\nShort note.\n\nAnother short note on the same page.\n\n"
            "# Requirements\n\n" + "The contractor shall provide the documents listed below. " * 12)
    chunks = Chunker(TokenCounter(), chunk_tokens=120, min_tokens=30).chunk([PageForChunking(1, page)], HeaderFields())
    assert "Short note." in chunks[0].text and "Another short note" in chunks[0].text  # merged into one chunk
    assert chunks[0].section_path == "Scope"
    assert all(c.section_path == "Requirements" for c in chunks[1:]) and len(chunks) >= 2


def test_structured_documents_do_not_guess_headings_and_drawings_keep_labels_together() -> None:
    body = "# Part A\nBEDROOM\n4000 x 3600\nBALCONY\n8150 x 1680\n"
    chunks = Chunker(TokenCounter(), chunk_tokens=200, min_tokens=10).chunk(
        [PageForChunking(1, body + "# Part B\ntext of part b that is long enough to stand alone as a chunk of its own.")],
        HeaderFields())
    assert chunks[0].section_path == "Part A" and "BEDROOM" in chunks[0].text  # 'BEDROOM' is not a heading here
    drawing = PageForChunking(2, "\n".join(["BEDROOM", "4000 x 3600", "BALCONY", "8150 x 1680", "LAGOON"]), is_drawing=True)
    sheet = Chunker(TokenCounter(), chunk_tokens=200).chunk([drawing], HeaderFields())
    assert len(sheet) == 1 and sheet[0].section_path == ""


def test_symbol_font_bullets_become_bullets_and_bracket_pieces_vanish() -> None:
    assert clean_text("factors:\n Functional impact\n High contrast") == "factors:\n• Functional impact\n• High contrast"
    assert clean_text("A =\n\nx2\n") == "A =\n\nx2"
    assert clean_text("a b") == "a b"


def test_a_question_naming_a_table_or_figure_is_an_exact_lookup_but_body_references_are_not() -> None:
    from rag.metadata.identifiers import identifier_keys, label_keys

    assert identifier_keys("give me the elements of Table 11", query=True) == ["TABLE-11"]
    assert identifier_keys("what does fig. 3 show and appendix 2?", query=True) == ["FIGURE-3", "APPENDIX-2"]
    assert identifier_keys("table of contents", query=True) == []
    assert label_keys("Table 11: self-contained year-end statement (multiple problems resolved)") == ["TABLE-11"]
    assert label_keys("Chapter 3 > Table 04.2: totals") == ["TABLE-4.2"]
    # a sentence that merely mentions a table is a reference, not a caption: documents only take labels from heading paths
    assert "TABLE-3" not in identifier_keys("As shown in Table 3, costs rose.")


def test_a_caption_directly_above_a_table_titles_it_in_any_file_format_but_prose_does_not() -> None:
    tables = [{"rows": [["Item", "2011"], ["Income", "250,000"], ["Costs", "10,000"]], "header_rows": 1}]
    page = PageForChunking(1, "Annual report\nTable 11: year-end statement\n[[TABLE:0]]", tables=tables)
    chunks = Chunker(TokenCounter(), chunk_tokens=200).chunk([page], HeaderFields())
    titled = [c for c in chunks if c.kind == "table"]
    assert titled and titled[0].section_path == "Table 11: year-end statement" and "TABLE-11" in titled[0].identifiers
    assert not any("Annual report" in c.text and c.kind == "table" for c in chunks)  # the caption no longer glues onto the paragraph above
    prose = PageForChunking(1, "Table 3 shows that costs rose by 5% this year.\n[[TABLE:0]]", tables=tables)
    assert Chunker(TokenCounter(), chunk_tokens=200).chunk([prose], HeaderFields())[0].section_path == ""
    alone = PageForChunking(1, "Table 3: a note that stands alone\nSome following paragraph text here.")
    assert Chunker(TokenCounter(), chunk_tokens=200).chunk([alone], HeaderFields())[0].section_path == ""


def test_a_bold_label_with_its_values_that_was_taken_for_a_heading_is_not_lost_from_the_text() -> None:
    page = PageForChunking(1, "# Application\n##### Trade licence no.: 784199012345671 info@acme.example 0501234567 address\nThe applicant signs below.")
    chunks = Chunker(TokenCounter(), chunk_tokens=300).chunk([page], HeaderFields())
    body = " ".join(c.text for c in chunks)
    assert "info@acme.example" in body and "784199012345671" in body and "0501234567" in body
    plain = PageForChunking(1, "# Signs of an Incident\nText follows here.")
    assert "Signs of an Incident" not in " ".join(c.text for c in Chunker(TokenCounter(), chunk_tokens=300).chunk([plain], HeaderFields()))


def test_every_passage_of_a_spreadsheet_carries_its_sheet_name_not_only_the_first() -> None:
    rows = [["Item", "Description", "Amt"]] + [[str(i), f"Supply and install cable run {i} on level {i % 5}", str(1000 + i)] for i in range(120)]
    rows += [["", "TOTAL", "326685"], ["", "ADD VAT 5%", "16334.25"], ["", "TOTAL", "343019.25"]]
    sheets = [PageForChunking(n, f"Sheet: {name}\n[[TABLE:0]]", tables=[{"rows": rows, "header_rows": 1}]) for n, name in ((1, "S3T"), (2, "ORIENT"))]
    chunks = Chunker(TokenCounter(), chunk_tokens=300).chunk(sheets, HeaderFields())
    assert len(chunks) > 4
    for chunk in chunks:
        assert chunk.section_path in {"Sheet: S3T", "Sheet: ORIENT"} and chunk.section_path in chunk.header, chunk.section_path
    last_s3t = [c for c in chunks if c.section_path == "Sheet: S3T"][-1]
    assert "343019.25" in last_s3t.text and "S3T" in last_s3t.header   # the tail passage with the total now says which quote it belongs to


def test_a_long_header_is_trimmed_to_fit() -> None:
    chunker = Chunker(TokenCounter(), chunk_tokens=300)
    fields = HeaderFields(project="MT", doc_number="03 30 00", title="A very long document title " * 20)
    header = chunker.header(fields, "Sheet: " + "S3T REV " * 30)
    assert header.startswith("MT | 03 30 00 | A very long") and chunker.counter.count(header) <= chunker.header_tokens + 5
