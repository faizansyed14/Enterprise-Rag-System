"""The full AutoCAD sheet analysis: its own prompt, its own model, the sheet cropped and cut into labelled tiles, one request."""
import asyncio
import io
from types import SimpleNamespace

import ezdxf
import pytest

from rag.ingestion import cad, cad_analysis
from rag.ingestion.formats import read_cad


def _drawing(second_plan: bool = False) -> bytes:
    """A plan (model space, enough objects to be worth analysing), a layout that holds only a frame and a title block (its
    viewport shows nothing), and optionally a second real plan on another layout."""
    doc = ezdxf.new("R2018", setup=True)
    msp = doc.modelspace()
    for i in range(320):
        x, y = (i % 40) * 10, (i // 40) * 12
        msp.add_lwpolyline([(x, y), (x + 8, y), (x + 8, y + 8), (x, y + 8)], close=True, dxfattribs={"color": 1 if i < 3 else 7})
    msp.add_text("Ground Floor Plan", dxfattribs={"height": 5}).set_placement((0, 120))
    msp.add_text("Carrefour Market G18 128 Sqm", dxfattribs={"height": 3}).set_placement((20, 60))
    block = doc.blocks.new("TITLEBLOCK")
    block.add_line((0, 0), (100, 0))
    for tag, y in (("DRAWING_TITLE", 30), ("DWG-NO", 20)):
        block.add_attdef(tag, (0, y))
    sheet = doc.layouts.new("A 101")
    sheet.add_blockref("TITLEBLOCK", (0, 0)).add_auto_attribs({"DRAWING_TITLE": "MASTER GROUND FLOOR PLAN", "DWG-NO": "A-101"})
    if second_plan:
        other = doc.layouts.new("B 202")
        for i in range(320):
            x, y = (i % 40) * 10, (i // 40) * 12
            other.add_lwpolyline([(x, y), (x + 8, y), (x + 8, y + 8), (x, y + 8)], close=True)
        other.add_text("First Floor Plan", dxfattribs={"height": 5}).set_placement((0, 120))
    buffer = io.StringIO()
    doc.write(buffer)
    return buffer.getvalue().encode("utf-8")


def _png(width, height, boxes):
    from PIL import Image, ImageDraw

    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, fill=0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_a_sheet_is_cropped_and_cut_into_labelled_overlapping_tiles():
    from PIL import Image

    # the drawing fills only the left part of a wide page: the blank part is cropped away before the cut
    png = _png(6000, 4000, [(100, 100, 1800, 3900), (400, 400, 1500, 600), (900, 2500, 1700, 3700)])
    overview, tiles, grid = cad_analysis.make_tiles(png, "4x3")
    assert grid == (3, 4)  # the cropped content is taller than wide, so 4 columns x 3 rows is turned round
    assert max(Image.open(io.BytesIO(overview)).size) <= cad_analysis.MAX_TILE_PX
    assert all(max(Image.open(io.BytesIO(t.png)).size) <= cad_analysis.MAX_TILE_PX for t in tiles)
    assert {t.label for t in tiles} <= {f"R{r}C{c}" for r in range(1, 5) for c in range(1, 4)}
    assert any(t.label == "R1C1" for t in tiles)


def test_a_wide_drawing_keeps_four_columns_and_three_rows_and_blank_tiles_are_left_out():
    solid = _png(6000, 4000, [(0, 0, 5999, 3999)])
    _, tiles, grid = cad_analysis.make_tiles(solid, "4x3")
    assert grid == (4, 3) and len(tiles) == 12
    corners = _png(6000, 4000, [(0, 0, 300, 300), (5700, 3700, 5999, 3999)])  # nothing in the middle
    _, tiles, _ = cad_analysis.make_tiles(corners, "4x3")
    assert 0 < len(tiles) < 12 and {t.label for t in tiles} >= {"R1C1", "R3C4"}
    with pytest.raises(ValueError):
        cad_analysis.make_tiles(_png(2000, 1000, []), "4x3")
    assert cad_analysis.parse_grid("2x2") == (2, 2) and cad_analysis.parse_grid("nonsense") == (4, 3)


def test_neighbouring_tiles_overlap_so_a_label_on_an_edge_is_whole_in_one_of_them():
    from PIL import Image

    # a solid 4,000 px page: four columns that each start 90% of a tile after the last
    _, tiles, _ = cad_analysis.make_tiles(_png(4000, 2800, [(0, 0, 3999, 2799)]), "4x3")
    first, second = (Image.open(io.BytesIO(t.png)).size for t in tiles[:2])
    assert first == second and abs(first[0] - 4000 / (1 + 3 * 0.9)) < 6 and abs(first[1] - 2800 / (1 + 2 * 0.9)) < 6


def test_one_request_carries_the_prompts_the_overview_and_every_labelled_tile():
    png = _png(6000, 4000, [(0, 0, 5999, 3999)])
    overview, tiles, grid = cad_analysis.make_tiles(png, "4x3")
    system, user = cad_analysis.build_messages("Carrefour Market 128 Sqm", overview, tiles, grid, send_text=True,
                                               other_sheets="Sheet: A 101: title: MASTER GROUND FLOOR PLAN; doc_number: A-101")
    assert system.role == "system" and "architectural drawing analyst" in system.content and '"n/s"' in system.content
    parts = user.content
    assert parts[0]["text"].startswith("Attached is a CAD drawing. Image 1 is the full overview.")
    assert "Tenant schedule" in parts[0]["text"] and "Be exhaustive. Read every label on every tile" in parts[0]["text"]
    assert sum(p["type"] == "image" for p in parts) == 1 + len(tiles)  # the overview first, then every tile
    labels = [p["text"] for p in parts if p["type"] == "text" and p["text"].startswith("Image ")]
    assert labels[0].startswith("Image 1") and "Tile R1C1 (row 1 of 3, column 1 of 4)" in labels[1]
    texts = [p["text"] for p in parts if p["type"] == "text"]
    assert any("<drawing_text>" in t and "Carrefour Market" in t for t in texts)
    assert texts[-1].endswith("</other_sheets>") and "MASTER GROUND FLOOR PLAN" in texts[-1]
    _, without = cad_analysis.build_messages("Carrefour Market", overview, tiles, grid, send_text=False)
    assert "<drawing_text>" not in "".join(p.get("text", "") for p in without.content)
    assert "<other_sheets>" not in "".join(p.get("text", "") for p in without.content)


REPORT = (
    "---\ntitle: Master Plan\ndocument_type: Tenancy plan\ndrawing_number: MP-01\nrevision: Not stated\nscale: 1:500\n"
    "project_name: n/s\n---\n\n## 2. Summary\nA mall with two wings. " + "Detail. " * 40
    + "\n\n## 3. Tenant schedule\n| Wing | Tenant no. | Occupant | Area | Unit code |\n|---|---|---|---|---|\n"
    "| A | 1 | Carrefour Market | 128 Sqm | G18 |\n"
)


class _Cad:
    """A model that answers with each of `replies` in turn (the last one repeats): (text, finish_reason)."""

    def __init__(self, replies=((REPORT, "stop"),), error=None):
        self.calls, self.replies, self.error = [], list(replies), error

    async def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if self.error:
            raise self.error
        text, finish = self.replies[min(len(self.calls), len(self.replies)) - 1]
        return SimpleNamespace(content=text, finish_reason=finish, usage=SimpleNamespace(output_tokens=kwargs["max_tokens"]))


class _Vision:
    def __init__(self):
        self.messages = []

    async def complete(self, messages, **kwargs):
        self.messages.append(messages)
        return SimpleNamespace(content='{"title": "Plan", "drawing_number": "", "summary": "A short description."}')


def _analysed(model, *, drawing=None, sheets=2, **kwargs):
    parsed, images, hires = cad.read_full(drawing or _drawing(), "dxf", analysis_sheets=sheets, analysis_px=2400)
    done = asyncio.run(cad_analysis.analyze_sheets(parsed, hires, cad_analysis.Options(model=model), **kwargs))
    return parsed, images, done


def test_the_plan_is_analysed_and_the_empty_layout_is_not():
    model = _Cad()
    parsed, _, done = _analysed(model)
    assert done == {0} and len(model.calls) == 1  # the layout with only a frame and a title block is not sent
    messages, kwargs = model.calls[0]
    assert kwargs["temperature"] == 0.0 and kwargs["max_tokens"] == 16000 and kwargs["timeout"] == 300.0
    assert sum(p["type"] == "image" for p in messages[1].content) >= 2
    model_space, layout = parsed.pages
    assert cad_analysis.MARKER in model_space.text and "| A | 1 | Carrefour Market | 128 Sqm | G18 |" in model_space.text
    assert model_space.method == "cad+vision" and layout.method == "cad" and cad_analysis.MARKER not in layout.text
    assert model_space.title_block["title"] == "Master Plan" and model_space.title_block["doc_number"] == "MP-01"
    assert "revision" not in model_space.title_block and "project" not in model_space.title_block  # "Not stated", "n/s"
    assert layout.title_block["title"] == "MASTER GROUND FLOOR PLAN"  # the drawing's own title block is not overwritten


def test_the_title_block_of_the_other_sheets_is_given_to_the_model_with_the_plan():
    model = _Cad()
    _analysed(model)
    texts = [p["text"] for p in model.calls[0][0][1].content if p["type"] == "text"]
    other = next(t for t in texts if "<other_sheets>" in t)
    assert "Sheet: A 101" in other and "MASTER GROUND FLOOR PLAN" in other and "A-101" in other


def test_two_real_plans_are_both_analysed_up_to_the_configured_number():
    parsed, images, hires = cad.read_full(_drawing(second_plan=True), "dxf", analysis_sheets=1, analysis_px=2400)
    assert [h is not None for h in hires] == [True, False, False] and all(images)
    parsed, images, hires = cad.read_full(_drawing(second_plan=True), "dxf", analysis_sheets=2, analysis_px=2400)
    assert [h is not None for h in hires] == [True, False, True]  # the empty layout is skipped, not counted
    _, _, none = cad.read_full(_drawing(), "dxf")
    assert none == [None, None]


def _red_pixels(png):
    from PIL import Image

    image = Image.open(io.BytesIO(png)).convert("RGB")
    return sum(1 for r, g, b in image.getdata() if r > 200 and g < 60 and b < 60)


def test_the_sheet_is_shown_in_its_own_colours_in_the_viewer_and_the_analysis_unless_colour_is_switched_off():
    data = _drawing()
    doc = cad.load(data, "dxf")
    layout = cad.sheets(doc)[0][1]
    assert _red_pixels(cad.render(doc, layout, width_px=1600)) > 20  # the default: the drawing's own colours
    assert _red_pixels(cad.render(doc, layout, width_px=1600, colour=False)) == 0  # black lines and outlines
    parsed, images = cad.read(data, "dxf")  # the picture made while the file is read is the viewer's first look
    assert _red_pixels(images[0]) > 20
    _, black, _ = cad.read_full(data, "dxf", preview_colour=False)
    assert _red_pixels(black[0]) == 0
    assert _red_pixels(cad.render_page(data, "dxf", 1, 1.0)) > 20  # another zoom level, drawn from the stored file
    assert _red_pixels(cad.render_page(data, "dxf", 1, 1.0, colour=False)) == 0


def test_reprocessing_replaces_every_stored_zoom_level_of_a_sheet_so_no_old_black_and_white_copy_is_shown(tmp_path):
    sha = "ab" * 32
    folder = tmp_path / "pages" / sha[:2]
    folder.mkdir(parents=True)
    old = [folder / f"{sha}-1-{scale}.png" for scale in ("1.00", "1.25", "1.50", "2.00")]
    other_sheet = folder / f"{sha}-2-1.00.png"
    for path in [*old, other_sheet]:
        path.write_bytes(b"old black and white")
    _, images = cad.read(_drawing(), "dxf")
    cad.prime_previews(tmp_path, sha, [images[0], None])
    assert not old[0].exists() and not old[3].exists()  # the zoom levels that were drawn before are gone
    assert old[1].read_bytes()[:4] == b"\x89PNG" and old[2].read_bytes()[:4] == b"\x89PNG"  # the two primed ones are new
    assert other_sheet.exists()  # a sheet with no new picture keeps what it had


def test_the_report_is_cut_into_searchable_chunks_with_its_table_rows_labelled():
    from rag.ingestion.chunker import Chunker, HeaderFields, PageForChunking
    from rag.text.tokens import TokenCounter

    parsed, _, _ = _analysed(_Cad())
    page = parsed.pages[0]
    chunks = Chunker(TokenCounter(None)).chunk(
        [PageForChunking(number=1, text=page.text, tables=[], label=page.label, is_drawing=True, title_block={}, language=None)],
        HeaderFields(project="", doc_number="", title="Master Plan"))
    joined = "\n".join(c.text for c in chunks)
    assert "Carrefour Market" in joined and "Unit code: G18" in joined  # the schedule's row keeps its column names


def test_an_empty_reply_is_asked_for_again_with_twice_the_output_limit():
    model = _Cad(replies=(("", "length"), (REPORT, "stop")))
    parsed, _, done = _analysed(model)
    assert done == {0} and len(model.calls) == 2
    assert model.calls[1][1]["max_tokens"] == 2 * model.calls[0][1]["max_tokens"] == 32000
    assert cad_analysis.MARKER in parsed.pages[0].text


def test_a_long_report_cut_off_at_the_limit_is_kept_and_a_model_that_never_writes_one_fails():
    model = _Cad(replies=((REPORT, "length"),))
    parsed, _, done = _analysed(model)
    assert done == {0} and len(model.calls) == 1  # most of the report is there: no second, costly request
    model = _Cad(replies=(("", "length"),))
    parsed, _, done = _analysed(model)
    assert done == set() and len(model.calls) == 2 and sum("sheet analysis failed" in w for w in parsed.warnings) == 1


def test_a_sheet_whose_analysis_fails_keeps_its_text_and_is_left_for_the_short_description():
    for model in (_Cad(error=RuntimeError("429")), _Cad(replies=(("I cannot help with that.", "stop"),))):
        parsed, _, done = _analysed(model)
        assert done == set() and all(cad_analysis.MARKER not in p.text for p in parsed.pages)
        assert sum("sheet analysis failed" in w for w in parsed.warnings) == 1


def test_no_drawing_is_sent_when_the_budget_is_used_up_or_the_text_holds_an_account_number():
    async def no_budget(bucket, amount):
        return False

    model = _Cad()
    parsed, _, done = _analysed(model, take_budget=no_budget)
    assert model.calls == [] and done == set() and any("budget" in w for w in parsed.warnings)
    parsed, _, hires = cad.read_full(_drawing(), "dxf", analysis_sheets=2, analysis_px=2400)
    parsed.pages[0].text += "\nIBAN: AE070331234567890123456"
    done = asyncio.run(cad_analysis.analyze_sheets(parsed, hires, cad_analysis.Options(model=model), protect_kinds=("iban",)))
    assert done == set() and model.calls == [] and any("bank or id" in w for w in parsed.warnings)


def test_the_cad_flow_replaces_the_short_description_and_falls_back_to_it():
    class Reader:
        def __init__(self, options):
            self.vision, self.cad = _Vision(), options
            self.take_budget = self.protect_kinds = None

    reader = Reader(cad_analysis.Options(model=_Cad(), render_px=2400))
    parsed = asyncio.run(read_cad(_drawing(), "dxf", reader))
    plan, layout = parsed.pages
    assert cad_analysis.MARKER in plan.text and "[Sheet description]" not in plan.text  # the plan is not described twice
    assert len(reader.vision.messages) == 1 and "[Sheet description]" in layout.text  # the empty layout keeps the short one
    reader = Reader(cad_analysis.Options(model=_Cad(error=RuntimeError("down")), render_px=2400))
    parsed = asyncio.run(read_cad(_drawing(), "dxf", reader))
    assert len(reader.vision.messages) == 2 and all("[Sheet description]" in p.text for p in parsed.pages)
    reader = Reader(None)  # CAD_VISION=none or no key: exactly what happened before this flow existed
    parsed = asyncio.run(read_cad(_drawing(), "dxf", reader))
    assert len(reader.vision.messages) == 2 and all(cad_analysis.MARKER not in p.text for p in parsed.pages)


def test_the_hub_takes_the_cad_model_from_the_cad_vision_role_and_has_none_when_it_is_off():
    from rag.plugins.hub import ModelHub

    from rag.config.roles import parse_role
    from rag.errors import ConfigurationError

    class Hub(ModelHub):
        def __init__(self, settings, line, fails=False):  # no plugin loading needed here
            self.settings, self.line, self.fails, self.asked, self._unusable_logged = settings, line, fails, [], set()

        def spec(self, role):
            return parse_role(role, self.line)

        def get(self, role, line=None):
            if self.fails:
                raise ConfigurationError("no key")
            self.asked.append((role, line))
            return _Cad()

    s = SimpleNamespace(cad_tiles="4x3", cad_render_px=6000, cad_max_sheets=3, cad_send_text=True, cad_max_tokens=16000,
                        cad_colour=True, cad_reasoning="low", cad_timeout_s=300.0)
    sonnet = "openrouter/anthropic/claude-sonnet-5.5"
    hub = Hub(s, sonnet)
    options = hub.cad_options()
    assert hub.asked == [("CAD_VISION", sonnet + "?reasoning=low")]  # OpenRouter is asked to keep the hidden thinking short
    assert options.max_sheets == 3 and options.tiles == "4x3" and options.colour and options.model is not None
    hub = Hub(s, sonnet + "?reasoning=high")  # a choice made on the model line is respected
    hub.cad_options()
    assert hub.asked == [("CAD_VISION", sonnet + "?reasoning=high")]
    s.cad_reasoning = ""
    hub = Hub(s, sonnet)
    hub.cad_options()
    assert hub.asked == [("CAD_VISION", sonnet)]
    s.cad_reasoning = "low"
    hub = Hub(s, "anthropic/claude-sonnet-5-5")  # the reasoning option is OpenRouter's
    hub.cad_options()
    assert hub.asked == [("CAD_VISION", "anthropic/claude-sonnet-5-5")]
    assert Hub(s, "none").cad_options() is None  # role off
    assert Hub(s, sonnet, fails=True).cad_options() is None  # no key: sheets get the short description


def test_the_tiles_keep_the_drawings_colours():
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (4000, 2800), "white")
    ImageDraw.Draw(image).rectangle((200, 200, 3800, 2600), fill=(255, 255, 0))  # a yellow, allocated unit
    ImageDraw.Draw(image).rectangle((2000, 1000, 2600, 1600), fill=(0, 160, 255))  # a blue one
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    overview, tiles, _ = cad_analysis.make_tiles(buffer.getvalue(), "4x3")

    def colours(png):
        pixels = Image.open(io.BytesIO(png)).convert("RGB").getdata()
        return sum(1 for r, g, b in pixels if r > 200 and g > 200 and b < 80), sum(1 for r, g, b in pixels if b > 200 and r < 60)

    yellow, blue = colours(overview)
    assert yellow > 1000 and blue > 100  # the overview is not grey-scale
    assert sum(colours(t.png)[0] for t in tiles) > 1000 and sum(colours(t.png)[1] for t in tiles) > 100


def test_a_report_cut_off_inside_its_code_fence_is_still_read_as_front_matter():
    cut = "```markdown\n---\ntitle: \"Ground Floor Plan (title in drawing: 'Ground Floor Plan'; sheet title: 'MASTER')\"\n" \
          "drawing_number: \"A11-02 (from the supplied title-block data; not printed on the plan itself)\"\n" \
          "project_name: \"Not stated (project number 25860 appears only in the supplied data)\"\nrevision: Not stated\n" \
          "scale: '1:250 (from the supplied data)'\n---\n\n## 2. Summary\nA mall."
    assert cad_analysis._strip_fence(cut).startswith("---\ntitle:")
    assert cad_analysis._strip_fence("```markdown\n---\nx: 1\n---\nbody\n```") == "---\nx: 1\n---\nbody"
    assert cad_analysis.front_matter(cad_analysis._strip_fence(cut)) == {
        "title": "Ground Floor Plan", "doc_number": "A11-02", "scale": "1:250"}  # remarks in brackets are not part of a value
    says_nothing = ('---\ndrawing_number: "Not stated on this sheet (the attributes are placeholders)"\n'
                    'revision: "Not present on this sheet (the other sheet shows Rev 05)"\nscale: n/s\nproject_name: "Not on the plan"\n'
                    'title: Master plan\n---\n')
    assert cad_analysis.front_matter(says_nothing) == {"title": "Master plan"}  # "not stated ..." is never a document number


def test_the_text_written_on_the_other_sheets_goes_with_the_plan_because_the_project_and_owner_are_there():
    parsed, _, _ = cad.read_full(_drawing(), "dxf")
    layout = parsed.pages[1]
    layout.text += "\n\nText on the drawing:\nAL SEEF VELLAGE\nOWNER: SHEIKH SULTAN\nMASTER DEVELOPER"
    note = cad_analysis.other_sheets_note(parsed, 0)
    assert "MASTER GROUND FLOOR PLAN" in note and "AL SEEF VELLAGE" in note and "SHEIKH SULTAN" in note
    assert cad_analysis.other_sheets_note(parsed, 1) == "" or "Model space" in cad_analysis.other_sheets_note(parsed, 1)


def test_cad_vision_is_a_vision_role_with_sonnet_by_default_that_the_models_page_can_switch():
    from rag.config.roles import ROLE_DESCRIPTIONS, ROLES, UI_SWITCHABLE_ROLES, Capability, parse_role
    from rag.config.settings import Settings

    assert ROLES["CAD_VISION"] is Capability.VISION and "CAD_VISION" in UI_SWITCHABLE_ROLES and ROLE_DESCRIPTIONS["CAD_VISION"]
    spec = Settings(_env_file=None).role("CAD_VISION")
    assert spec.label == "openrouter/anthropic/claude-sonnet-5.5" and "opus" not in spec.label
    assert parse_role("CAD_VISION", "openrouter/anthropic/claude-haiku-5.5").capability is Capability.VISION
    assert parse_role("CAD_VISION", "openrouter/anthropic/claude-sonnet-5.5?reasoning=low").options == {"reasoning": "low"}


def test_an_openrouter_model_line_can_limit_the_models_hidden_reasoning():
    from rag.config.roles import parse_role
    from rag.providers.llm.openai_compat import OpenAICompatChat

    def body(line):
        chat = OpenAICompatChat.__new__(OpenAICompatChat)  # only what _body reads
        chat.provider, chat.spec = "openrouter", parse_role("CAD_VISION", line)
        chat.settings = SimpleNamespace(llm_answer_max_tokens=1000, llm_temperature=0.0, openrouter_zdr=False)
        return chat._body([], tools=None, max_tokens=500, temperature=0.0, json_mode=False, stream=False)

    assert body("openrouter/anthropic/claude-sonnet-5.5?reasoning=low")["reasoning"] == {"effort": "low"}
    assert "reasoning" not in body("openrouter/anthropic/claude-sonnet-5.5")  # nothing changes unless asked for
    assert "reasoning" not in body("openrouter/anthropic/claude-sonnet-5.5?reasoning=banana")
