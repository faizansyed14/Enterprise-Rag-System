from __future__ import annotations

from rag.plugins.capabilities import ParsedTable
from rag.providers.parsers.table_flow import link_continued_tables, repeats_heading

HEAD = ["Paper", "Domain", "Task"]


def _table(rows: list[list[str]], top: float, bottom: float, x0: float = 0.1, x1: float = 0.9) -> ParsedTable:
    return ParsedTable(rows=rows, header_rows=1, bbox=(x0, top, x1, bottom))


def test_a_table_that_runs_on_gets_the_heading_of_the_page_before() -> None:
    first = _table([HEAD, ["Kim (2018)", "CV", "classification"], ["Ren (2018)", "ML", "RL"]], 0.2, 0.92)
    second = _table([["Lietal. (2017)", "CV", "detection"], ["Wu (2018)", "NLP", "translation"]], 0.08, 0.5)
    assert link_continued_tables([[first], [second]]) == 1
    assert second.rows[0] == HEAD and second.header_rows == 1 and len(second.rows) == 3


def test_a_heading_printed_again_on_the_next_page_is_left_alone() -> None:
    first = _table([HEAD, ["Kim (2018)", "CV", "classification"]], 0.2, 0.92)
    second = _table([HEAD, ["Wu (2018)", "NLP", "translation"]], 0.08, 0.5)
    assert link_continued_tables([[first], [second]]) == 0
    assert len(second.rows) == 2


def test_the_heading_is_passed_down_a_chain_of_pages() -> None:
    pages = [[_table([HEAD, ["a1", "b1", "c1"]], 0.2, 0.95)],
             [_table([["a2", "b2", "c2"]], 0.05, 0.95)],
             [_table([["a3", "b3", "c3"]], 0.05, 0.6)]]
    assert link_continued_tables(pages) == 2
    assert pages[2][0].rows[0] == HEAD


def test_a_table_that_does_not_reach_the_page_end_or_start_is_not_a_continuation() -> None:
    first = _table([HEAD, ["Kim", "CV", "x"]], 0.2, 0.5)  # ends mid-page
    second = _table([["Wu", "NLP", "y"]], 0.08, 0.5)
    assert link_continued_tables([[first], [second]]) == 0
    first = _table([HEAD, ["Kim", "CV", "x"]], 0.2, 0.95)
    lower = _table([["Wu", "NLP", "y"]], 0.55, 0.9)  # starts mid-page
    assert link_continued_tables([[first], [lower]]) == 0


def test_a_different_shape_is_a_different_table() -> None:
    first = _table([HEAD, ["Kim", "CV", "x"]], 0.2, 0.95)
    other_columns = _table([["a", "b"], ["c", "d"]], 0.05, 0.5)
    shifted = _table([["Wu", "NLP", "y"]], 0.05, 0.5, x0=0.3, x1=0.95)
    assert link_continued_tables([[first], [other_columns]]) == 0
    assert link_continued_tables([[first], [shifted]]) == 0


def test_a_page_without_tables_breaks_the_chain() -> None:
    first = _table([HEAD, ["Kim", "CV", "x"]], 0.2, 0.95)
    later = _table([["Wu", "NLP", "y"]], 0.05, 0.5)
    assert link_continued_tables([[first], [], [later]]) == 0


def test_repeat_detection_ignores_case_and_punctuation() -> None:
    assert repeats_heading(["PAPER", "domain:", "Task"], HEAD)
    assert not repeats_heading(["Wu (2018)", "NLP", "translation"], HEAD)


def test_a_row_of_plain_labels_under_numeric_data_is_a_new_table_not_a_continuation() -> None:
    first = _table([["Item", "Qty", "Rate"], ["Cement", "120", "14.5"], ["Steel", "40", "310"]], 0.2, 0.95)
    new = _table([["Supplier", "Region", "Contact"], ["Acme", "North", "info"]], 0.05, 0.4)
    assert link_continued_tables([[first], [new]]) == 0


def test_each_table_is_placed_at_its_height_among_the_text_lines() -> None:
    from rag.providers.parsers.table_flow import interleave

    lines = [{"text": "Table 11: second", "top": 400, "x0": 50}, {"text": "Table 10: first", "top": 100, "x0": 50},
             {"text": "closing remark", "top": 700, "x0": 50}]
    # table 0 sits at 150 (under the first caption), table 1 at 450 (under the second); a table below all text goes last
    assert interleave(lines, [150, 450]).split("\n") == ["Table 10: first", "[[TABLE:0]]", "Table 11: second", "[[TABLE:1]]", "closing remark"]
    assert interleave(lines, [150, 800]).split("\n")[-1] == "[[TABLE:1]]"
    assert interleave(lines, [450, 150]).split("\n")[:3] == ["Table 10: first", "[[TABLE:1]]", "Table 11: second"]  # numbers follow storage order
    assert interleave([], []) == ""
