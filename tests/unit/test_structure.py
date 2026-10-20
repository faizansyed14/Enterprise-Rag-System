from __future__ import annotations

from rag.providers.parsers import structure
from rag.providers.parsers.structure import StyledLine
from rag.text.normalize import clean_text


def _page(number: int, body: list[StyledLine]) -> list[StyledLine]:
    header = StyledLine("NIST SP 800-61 COMPUTER SECURITY INCIDENT HANDLING GUIDE", 9.0)
    banner = StyledLine("This publication is available free of charge from: https://doi.org/10.6028/NIST", 9.0)
    return [header, StyledLine(str(number), 9.0), banner, *body]


def _document() -> list[list[StyledLine]]:
    pages = []
    for n in range(1, 9):
        body = [StyledLine("Organizations should prepare for incidents before they happen and keep the plan current.", 11.0)
                for _ in range(6)]
        if n == 3:
            body.insert(2, StyledLine("3.2.2 Signs of an Incident", 11.0, bold=True))
            body.insert(0, StyledLine("Incident Detection and Analysis", 16.0, bold=True))
        pages.append(_page(n, body))
    return pages


def test_running_headers_and_page_numbers_are_furniture() -> None:
    found = structure.analyse(_document())
    assert structure.key("NIST SP 800-61 COMPUTER SECURITY INCIDENT HANDLING GUIDE") in found.furniture
    assert structure.key("This publication is available free of charge from: https://doi.org/10.6028/NIST") in found.furniture
    assert found.labels[2] == "3"  # the printed number of page index 2


def test_headings_by_size_and_numbering_and_body_text_is_left_alone() -> None:
    found = structure.analyse(_document())
    assert found.headings["Incident Detection and Analysis"] == 1
    assert found.headings["3.2.2 Signs of an Incident"] == 3
    assert "Organizations should prepare for incidents before they happen and keep the plan current." not in found.headings
    text = "\n".join(["NIST SP 800-61 COMPUTER SECURITY INCIDENT HANDLING GUIDE", "3", "Incident Detection and Analysis",
                      "3.2.2 Signs of an Incident", "Organizations should prepare for incidents before they happen."])
    cleaned = structure.apply(text, found)
    assert cleaned.split("\n") == ["# Incident Detection and Analysis", "### 3.2.2 Signs of an Incident",
                                   "Organizations should prepare for incidents before they happen."]
    assert "#" not in structure.apply(text, found, headings=False)


def test_bold_run_in_leads_and_lowercase_lines_are_not_headings() -> None:
    pages = _document()
    pages[1].append(StyledLine("workforce with diverse skills and capabilities cost-effectively", 11.0, bold=True))
    pages[1].append(StyledLine("Overall, our survey makes the following contributions:", 11.0, bold=True))
    found = structure.analyse(pages)
    assert "workforce with diverse skills and capabilities cost-effectively" not in found.headings
    assert "Overall, our survey makes the following contributions:" not in found.headings


def test_outline_names_lines_even_when_a_glyph_is_missing() -> None:
    pages = ["Abstract\nsome text", "4.1 Heterogeneous Task Assignment Problem\nbody\n4.2 Question Assignment\nmore body",
             "Intro line\nA very long\nheading that wraps\nbody"]
    entries = [(1, "Abstract", 1), (2, "4.1 Heterogeneous Task Assignment Problem", 2), (2, "4.2 estion Assignment", 2),
               (1, "A very long heading that wraps", 3), (1, "Not in the text at all", 3)]
    texts, found = structure.mark_outline_headings(pages, entries)
    assert found == 4
    assert texts[0].split("\n")[0] == "# Abstract"
    assert "## 4.1 Heterogeneous Task Assignment Problem" in texts[1]
    assert "## 4.2 Question Assignment" in texts[1]  # the fuzzy match keeps the line's own (correct) spelling
    assert "# A very long heading that wraps" in texts[2] and "body" in texts[2]


def test_line_end_hyphenation_from_pdfium_is_joined() -> None:
    assert clean_text("approach to ob\x02tain a finite set") == "approach to obtain a finite set"
    assert clean_text("a 10\x02year plan, COVID\x0219") == "a 10-year plan, COVID-19"
    assert clean_text("perfor-\nmance of the system") == "performance of the system"
    assert clean_text("state-of-the-art method") == "state-of-the-art method"


def test_a_footer_fused_with_numbers_from_the_line_before_is_still_furniture() -> None:
    found = structure.analyse(_document())
    header = "NIST SP 800-61 COMPUTER SECURITY INCIDENT HANDLING GUIDE"
    assert structure.is_furniture(header, found.furniture)
    assert structure.is_furniture("13930 54 " + header, found.furniture)  # last table cell + the footer, one baseline
    assert structure.is_furniture(header + " 54", found.furniture)
    assert not structure.is_furniture("Form 13930 " + header, found.furniture)  # real words in front: content
    assert not structure.is_furniture("Organizations should prepare for incidents", found.furniture)
    cleaned = structure.apply("\n".join(["13930 54 " + header, "Organizations should prepare for incidents."]), found)
    assert cleaned == "Organizations should prepare for incidents."


def test_a_wrapped_heading_is_joined_and_a_new_sentence_is_not() -> None:
    found = structure.Structure(headings={"Table 11: self-contained year-end statement (multiple": 1, "Overview": 1}, body_size=11.0)
    wrapped = structure.apply("Table 11: self-contained year-end statement (multiple\nproblems resolved)\n[[TABLE:1]]", found)
    assert wrapped.split("\n") == ["# Table 11: self-contained year-end statement (multiple problems resolved)", "[[TABLE:1]]"]
    # a heading that ends its sentence, or a next line that starts a new one, is left alone
    assert structure.apply("Overview\nThe plan follows.", found).split("\n") == ["# Overview", "The plan follows."]
    found.headings["Overview:"] = 1
    assert structure.apply("Overview:\nlowercase start", found).split("\n") == ["# Overview:", "lowercase start"]  # a colon ends it
