from __future__ import annotations

from rag.answering.aggregate import AGGREGATE, _verdicts, subject_stems


def test_list_everything_questions_are_recognised_and_single_answer_questions_are_not() -> None:
    for q in ("tell me all the licenses with their expiry dates", "list all contracts with their end dates",
              "every permit we got from DED", "which documents mention the fit-out?"):
        assert AGGREGATE.search(q), q
    for q in ("What is the paint colour code used for the walls?", "Which law governs the Mutual NDA?", "What is the full name of the tenant?"):
        assert not AGGREGATE.search(q), q


def test_subject_stems_match_the_variants_in_file_names() -> None:
    assert subject_stems("all the licenses currently held by this company with expiry dates") == ["licens"]
    assert "contra" in subject_stems("list all contracts")


def test_a_cut_off_reply_keeps_its_complete_entries() -> None:
    raw = '{"0": {"relevant": true, "pages": [1, 2], "note": "licence"}, "1": {"relevant": false, "pages": [], "note": "menu"}, "2": {"relevant": tr'
    out = _verdicts(raw)
    assert out["0"] == {"relevant": True, "pages": [1, 2]} and out["1"]["relevant"] is False and "2" not in out
    assert _verdicts('{"0": {"relevant": true, "pages": [3], "note": "x"}}')["0"]["pages"] == [3]


def test_file_name_tokens_ignore_how_numbers_are_glued_to_letters() -> None:
    from rag.answering.strategy import name_tokens

    assert set(name_tokens("MAKAW Q 3 - 2022 VAT Return")) <= set(name_tokens("the Q3 2022 VAT return"))
