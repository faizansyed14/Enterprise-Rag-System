from __future__ import annotations

from rag.providers.parsers.table_quality import fill, is_usable, tidy


def test_columns_empty_throughout_are_dropped_and_no_text_is_lost() -> None:
    rows = [["Item", "", "Qty"], ["Cement", "", "120"], ["Steel", "", "40"]]
    assert tidy(rows) == [["Item", "Qty"], ["Cement", "120"], ["Steel", "40"]]


def test_fragments_of_one_column_are_joined() -> None:
    # a stray vertical rule cut the "Task" column in two; neither fragment is filled where the other is
    rows = [["Task", "", "Role"], ["", "M-1 Monitor", "Owner"], ["M-2", "", "Assessor"], ["M-3", "", "Officer"]]
    # the heading's "Task" and the data's fragments sit in different columns
    assert tidy(rows) == [["Task", "Role"], ["M-1 Monitor", "Owner"], ["M-2", "Assessor"], ["M-3", "Officer"]]


def test_two_columns_filled_in_the_heading_are_never_joined_even_if_the_rows_alternate() -> None:
    checklist = [["Requirement", "Compliant", "Non-compliant"], ["Fire doors", "X", ""], ["Exits", "", "X"], ["Alarms", "X", ""]]
    assert tidy(checklist) == checklist  # which column holds the X is the information


def test_wrapped_one_cell_fragments_make_a_table_unusable() -> None:
    shredded = [["", "RMF TASKS", "", "ROLES"], ["TASK M-1", "", "", ""], ["", "System changes", "", ""], ["", "", "", "Owner"],
                ["", "", "", "Officer"], ["", "monitor the", "", ""], ["", "environment", "", ""]]
    assert not is_usable(tidy(shredded))


def test_a_plain_grid_is_usable_and_fill_is_the_share_of_filled_cells() -> None:
    grid = [["Name", "Qty", "Rate"], ["Cement", "120", "14.5"], ["Steel", "40", "310"]]
    assert is_usable(tidy(grid)) and fill(grid) == 1.0
    assert not is_usable([["only one row"]])
