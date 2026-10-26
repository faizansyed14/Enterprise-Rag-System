"""AutoCAD drawings: recognised by their first bytes, read sheet by sheet (text, title block, layers, blocks), drawn, explained."""
import asyncio
import io
from types import SimpleNamespace

import ezdxf
import pytest

from rag.ingestion import cad
from rag.ingestion.formats import SUPPORTED, detect_format


def _drawing() -> bytes:
    """A small site plan: a plot outline, room names, a dimension, a title block as a block with attributes, and a few stray
    objects far away in the model space."""
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    doc.layers.add("A-WALL")
    doc.layers.add("A-TEXT")
    for i in range(30):  # enough objects to make a drawing
        msp.add_lwpolyline([(i * 10, 0), (i * 10 + 8, 0), (i * 10 + 8, 8), (i * 10, 8)], close=True, dxfattribs={"layer": "A-WALL"})
    msp.add_text("Ground Floor Plan", dxfattribs={"layer": "A-TEXT", "height": 5}).set_placement((0, 60))
    msp.add_text("Carrefour Market", dxfattribs={"layer": "A-TEXT", "height": 3}).set_placement((20, 30))
    msp.add_mtext("Starbucks\\P128 Sqm", dxfattribs={"layer": "A-TEXT", "insert": (100, 30)})
    msp.add_linear_dim(base=(0, -10), p1=(0, 0), p2=(298, 0)).render()
    for i in range(5):  # leftovers far from the plan, which must not shrink it into a corner
        msp.add_lwpolyline([(300_000 + i, 0), (300_010 + i, 0), (300_010 + i, 5)], close=True)
    block = doc.blocks.new("TITLEBLOCK")
    block.add_line((0, 0), (100, 0))
    for tag, y in (("DRAWING_TITLE", 30), ("DWG-NO", 20), ("SCALE", 10), ("REV", 0)):
        block.add_attdef(tag, (0, y))
    sheet = doc.layouts.new("A 101")
    ref = sheet.add_blockref("TITLEBLOCK", (0, 0))
    ref.add_auto_attribs({"DRAWING_TITLE": "MASTER GROUND FLOOR PLAN", "DWG-NO": "A-101", "SCALE": "1:250", "REV": "05"})
    buffer = io.StringIO()
    doc.write(buffer)
    return buffer.getvalue().encode("utf-8")


def test_dwg_and_dxf_are_recognised_by_their_first_bytes():
    assert cad.is_dwg(b"AC1027\x00\x00") and cad.is_dwg(b"AC1032")
    assert not cad.is_dwg(b"%PDF-1.7") and not cad.is_dwg(b"ACME00")
    assert cad.is_dxf(b"  0\r\nSECTION\r\n  2\r\nHEADER")
    assert detect_format(b"AC1027" + b"\x00" * 200, "Site Plan.dwg") == "dwg"
    assert detect_format(_drawing(), "plan.dxf") == "dxf"
    assert detect_format(_drawing(), "no-extension") == "dxf"  # by content, not by name
    assert {"dwg", "dxf"} <= SUPPORTED


def test_each_sheet_becomes_a_page_with_its_text_title_block_layers_and_blocks():
    parsed, images = cad.read(_drawing(), "dxf")
    assert [p.label for p in parsed.pages] == ["Model space", "Sheet: A 101"]
    model, sheet = parsed.pages
    assert all(p.is_drawing and p.method == "cad" for p in parsed.pages)
    assert "Ground Floor Plan" in model.text and "Carrefour Market" in model.text
    assert "Starbucks 128 Sqm" in model.text  # MTEXT paragraphs joined, formatting codes removed
    assert "298" in model.text  # the dimension's measured value
    assert "A-WALL" in model.text  # the layers say what the drawing is made of
    assert model.text.index("Ground Floor Plan") < model.text.index("Carrefour Market")  # top to bottom
    assert sheet.title_block == {"title": "MASTER GROUND FLOOR PLAN", "doc_number": "A-101", "scale": "1:250", "revision": "05"}
    assert "TITLEBLOCK" in sheet.text and "DWG-NO: A-101" in sheet.text
    assert parsed.title == "MASTER GROUND FLOOR PLAN"
    assert all(png and png[:8] == b"\x89PNG\r\n\x1a\n" for png in images)


def test_far_away_leftovers_are_left_out_of_the_frame():
    assert cad._main_range([float(v) for v in range(100)] + [300_000.0, 300_010.0]) == (0.0, 99.0)
    assert cad._main_range([float(v) for v in range(100)]) == (0.0, 99.0)  # nothing to drop
    halves = [float(v) for v in range(50)] + [1000.0 + v for v in range(50)]
    assert cad._main_range(halves) == (0.0, 1049.0)  # two real halves of one drawing stay together


def test_title_block_tags_are_matched_by_name_and_unknown_tags_fall_back_to_labels():
    assert cad.title_block_from({"PROJECT-ID": "25860", "SHEET-NO": "A11-02", "DRAWING_TITLE": "PLAN"}) == {
        "project_number": "25860", "sheet_number": "A11-02", "title": "PLAN"}
    assert cad.title_block_from({}) == {}


def test_a_stored_sheet_can_be_drawn_again_for_the_viewer():
    data = _drawing()
    assert cad.render_page(data, "dxf", 2, 1.0)[:4] == b"\x89PNG"
    with pytest.raises(ValueError):
        cad.render_page(data, "dxf", 9, 1.0)


class _Vision:
    def __init__(self):
        self.messages = []

    async def complete(self, messages, **kwargs):
        self.messages.append(messages)
        return SimpleNamespace(content='{"title": "Ground Floor Plan", "drawing_number": "A-101", "summary": "A mall plan."}')


def test_the_vision_model_explains_each_sheet_and_its_explanation_is_searchable():
    parsed, images = cad.read(_drawing(), "dxf")
    vision = _Vision()
    asyncio.run(cad.describe_sheets(parsed, images, vision))
    assert len(vision.messages) == 2
    parts = vision.messages[0][0].content
    assert [p["type"] for p in parts] == ["text", "text", "image"] and "Carrefour Market" in parts[1]["text"]
    assert "[Sheet description] A mall plan." in parsed.pages[0].text and parsed.pages[0].method == "cad+vision"
    assert parsed.pages[1].title_block["doc_number"] == "A-101"  # the drawing's own attribute is kept


def test_no_image_leaves_the_server_when_the_budget_is_used_up_or_the_text_holds_an_account_number():
    parsed, images = cad.read(_drawing(), "dxf")
    vision = _Vision()

    async def no_budget(bucket, amount):
        return False

    asyncio.run(cad.describe_sheets(parsed, images, vision, take_budget=no_budget))
    assert vision.messages == [] and any("budget" in w for w in parsed.warnings)
    parsed.pages[0].text += "\nIBAN: AE070331234567890123456"
    asyncio.run(cad.describe_sheets(parsed, images, vision, protect_kinds=("iban",)))
    assert len(vision.messages) == 1  # only the second sheet was sent
