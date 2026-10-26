from __future__ import annotations

from rag.answering.arithmetic import derived, unworked_sums


def test_an_unfinished_total_is_worked_out_exactly() -> None:
    assert unworked_sums("The total is 2240.7 + 2287.95 [1][2].") == ["2240.7 + 2287.95 = 4528.65"]
    assert unworked_sums("Paid 40,000 + 40,000 + 26,667 in cheques.") == ["40,000 + 40,000 + 26,667 = 106,667"]


def test_finished_sums_dates_and_phone_numbers_are_left_alone() -> None:
    assert unworked_sums("2240.7 + 2287.95 = 4528.65") == []
    assert unworked_sums("Call +971 50 123 4567 on 2025-01-03.") == []


def test_a_calculated_number_counts_as_supported() -> None:
    assert derived("11667", ["26667", "15000", "11667"])        # a difference
    assert derived("4528.65", ["2240.7", "2287.95", "4528.65"])  # a total
    assert not derived("9999", ["26667", "15000", "9999"])


def test_a_finished_chain_is_not_cut_into_a_wrong_partial_sum() -> None:
    assert unworked_sums("Rows 1 to 3 = 80 + 1350 + 2000 = 3430, then 164 + 216 + 3430 = 3810") == []
