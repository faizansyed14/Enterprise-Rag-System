from __future__ import annotations

from rag.answering.citations import verify
from rag.retrieval.types import Passage


def test_a_row_counter_the_answer_adds_to_a_table_is_not_checked_against_the_source() -> None:
    passage = Passage(n=1, chunk_id="c", version_id="v", document_id="d", doc_number="", title="Order Sheet",
                      page_start=1, page_end=1, section_path="", kind="table", text="Asiatique (noir) | IC0019 | 4 | 1.65", core_text="",
                      score=1.0, language="en", file_name="Order Sheet.xlsx", project="", doc_type="")
    found = verify("| 37 | Asiatique (noir) | IC0019 | 4 | [1] |", [passage])
    assert found and found[0].verified and not found[0].missing_numbers
    wrong = verify("| 37 | Asiatique (noir) | IC0019 | 9 | [1] |", [passage])
    assert wrong[0].missing_numbers == ["9"]
