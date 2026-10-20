from __future__ import annotations

from rag.text import ligatures
from rag.text.normalize import clean_text


def _protected(text: str) -> str:
    return clean_text(text, keep_glyph_codes=True)


def test_unmapped_glyph_codes_are_kept_until_the_document_is_read() -> None:
    kept = _protected("A\x1dribute")
    assert ligatures.has_placeholders(kept)
    assert clean_text("A\x1dribute") == "A ribute"  # the default still makes them spaces


def test_the_letters_behind_a_code_are_found_from_the_words_it_completes() -> None:
    page = _protected("The attribute A\x1dributes of each worker\x1d and the A\x1dribution of work. "
                      "Every pre\x1dy item has an a\x1dribute. Further a\x1dributes are listed.")
    mapping = ligatures.infer_mapping([page])
    code = chr(0xE000 + 0x1D)
    assert mapping[code] == "tt"
    assert "Attributes" in ligatures.repair_document([page])[0]


def test_tex_f_ligatures_are_confirmed_by_the_vocabulary() -> None:
    page = _protected("an e\x0eciency gain and e\x0ecient use; the di\x0berent o\x0bers and the \x0cnal \x0cle of \x0dow and \x0dood")
    fixed = ligatures.repair_document([page])[0]
    assert "efficiency" in fixed and "efficient" in fixed and "different" in fixed and "offers" in fixed
    assert "final" in fixed and "file" in fixed and "flow" in fixed


def test_a_code_nothing_explains_is_dropped_between_letters_and_spaced_elsewhere() -> None:
    page = _protected("zq\x1cxy and a \x1c b")
    fixed = ligatures.repair_document([page])[0]
    assert fixed == "zqxy and a b"


def test_a_lone_carriage_return_inside_a_word_is_the_fl_glyph_but_a_line_end_is_not() -> None:
    page = _protected("the \rood and the \rag and the \row of water over\row\r\nnext line")
    assert page.count(chr(0xE00D)) == 4 and page.count("\n") == 1
    assert ligatures.repair_document([page])[0].splitlines()[0] == "the flood and the flag and the flow of water overflow"


def test_text_without_placeholders_is_untouched() -> None:
    assert ligatures.repair_document(["plain text"]) == ["plain text"]


def test_a_name_repeated_on_every_page_does_not_outvote_a_real_word() -> None:
    names = " ".join(["Authors He\x1diachchi and Smith."] * 12)
    page = _protected(names + " Worker A\x1dributes are listed.")
    assert "Attributes" in ligatures.repair_document([page])[0]


def test_a_code_with_no_letters_around_it_is_never_taken_for_a_ligature() -> None:
    page = _protected("items \x16 first \x16 second \x16 third \x16 and \x16 more")
    assert ligatures.infer_mapping([page]) == {}
    assert ligatures.repair_document([page])[0] == "items first second third and more"


def test_two_candidates_that_both_make_words_need_agreeing_evidence() -> None:
    page = _protected("the A\x1dribute and the su\x1dix")  # one word each; "tt" explains the first only
    mapping = ligatures.infer_mapping([page])
    assert mapping.get(chr(0xE01D)) in (None, "tt")
