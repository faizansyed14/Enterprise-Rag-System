# Reading documents: parsers, OCR and tables

The `PARSER` and `OCR` roles decide how a file becomes text. This page records how the default was chosen, what was
measured, and what is still open.

## The default route (`PARSER=local/pdfium`, `OCR=local/rapidocr`)

Every PDF page gets the cheapest method that reads it correctly:

| Page | Method |
|---|---|
| Good text layer | pdfium text in reading order; ruled tables are extracted separately with pdfplumber so each row stays together |
| Text layer plus a large picture (a table or form pasted in as an image) | text layer, **plus OCR of each picture** (`OCR_IMAGE_MIN_FRACTION`, default 4% of the page) |
| Scanned or broken text layer | the page is rendered and OCR'd; results are cached by image hash |
| Mostly a picture with a little text (stamped scan) | text layer and full-page OCR merged |
| Drawing sheet (A3 and larger) | title block read from its region; a vision model describes sheets with little text (`VISION`) |

Word, Excel, PowerPoint, e-mail, HTML and images are read natively; images go straight to OCR. The output is plain
text with tables kept as rows, which the chunker splits along headings and table rows.

## AutoCAD drawings (`.dwg`, `.dxf`)

`ingestion/cad.py` reads drawings directly. They are not printed to PDF first.

1. **Recognise.** A DWG starts with `AC10` plus digits (the format version). A DXF is text that opens with a `SECTION`
   group. Both are recognised by content, not by name.
2. **Convert.** A DWG is converted to DXF by LibreDWG's `dwg2dxf`, which is built into the backend image. The converter
   runs as a separate process with a fixed argument list and a 180 s timeout. The DXF is then read with ezdxf's
   `recover` mode, which tolerates damaged files.
3. **One page per sheet.** Model space becomes one page and every paper-space layout another. Each page holds:
   - the sheet's text, top to bottom (TEXT, MTEXT with formatting codes removed, dimension values, block attributes);
   - the layers and blocks it uses;
   - a title block mapped from attribute tags (`DRAWING_TITLE` becomes title, `DWG-NO` becomes doc_number, then scale,
     revision, project number, sheet number …). Model space keeps a title block only when it has a title or a number.
4. **Draw.** Each sheet is drawn with ezdxf's SVG backend (white background, black lines, hatches as outlines) on an A3
   page and turned into a PNG with cairosvg. The PNG is the viewer's page image, and it is pre-rendered at extraction.
   Objects far outside the drawing are left out of the frame (gap-based trimming in `_main_range`).
5. **Analyse (its own flow, `ingestion/cad_analysis.py`, model `CAD_VISION`).** The sheets worth analysing are drawn
   **again at high resolution and in colour** (`CAD_RENDER_PX`, 6,000 px, about 360 dpi on A3; layer colours and solid
   fills kept, because colour often means allocated, vacant or a zone). The blank margin is cropped away and the sheet is
   cut into overlapping tiles (`CAD_TILES`, 4×3; a tall drawing is turned round; all-white tiles are dropped).
   **One** request carries a system prompt and a user prompt written for architectural drawings, the full overview,
   every tile labelled `R<row>C<col>`, the text read from the file (to check the spelling of labels) and what the file's other
   sheets state: their title-block
   fields and the start of their text, where the project name and owner usually are. The reply is a Markdown report: YAML front matter, summary, tenant schedule, service
   rooms, circulation, external works, detail references, and gaps. Values in its front matter lose any remark in brackets
   before they become the title or number. It is added to the sheet's text as `[Sheet analysis]`,
   so its headings and its table rows are chunked and searched like any other page, and its front matter fills the title,
   drawing number, revision, scale and project.
   - **Which sheets:** the first `CAD_MAX_SHEETS` (3) that have text and at least 300 objects. A layout that holds only a
     frame and a title block is skipped: analysing it would produce a report full of "not stated".
   - **Empty or cut-off reply:** a reply too short to be a report (the model spent its output allowance on hidden
     reasoning) is requested again once with the limit doubled. A long report cut at the limit is kept.
   - **Cost and limits:** about 13 images per sheet; the daily `VISION_PAGES_PER_DAY` budget applies; a sheet whose text
     holds a protected value (an IBAN, for example) is never sent.
   - **Model:** chosen on the Models page ("AutoCAD drawing analysis") or with `CAD_VISION`. OpenRouter models are asked for
     low hidden reasoning by default (`CAD_REASONING`), because thinking counts against the output limit: on a site plan
     14,700 of 16,000 tokens went on it and the report was cut off in its tenant table. `?reasoning=` on a model line
     overrides it.
6. **Short description (fallback).** Sheets that were not analysed (beyond the limit, empty, or the analysis failed) get
   the earlier short description from the `VISION` model: a title, a drawing number and a summary, added as
   `[Sheet description] …`. With `CAD_VISION=none` every sheet gets only this.

Limits:

- a paper-space viewport is drawn empty;
- hatches are drawn as outlines;
- external references (xrefs) are not loaded.

## OCR spacing

OCR output from RapidOCR often drops spaces ("rescheduled to24March2026"). `rag.text.ocr_spacing` puts them back with a
word-frequency model while leaving identifiers, codes (C40, SI-014), numbers and short capitalised names alone.

## What the PDF reader repairs after extracting

Plain text extraction returns what the file says, not what the page shows. These repairs run on every PDF; each came from a
defect found in a real document of the evaluation library.

| Defect | Seen as | Repair |
|---|---|---|
| **Ligatures the font cannot name** (ff, fi, fl, ffi, tt, Qu ...) | "A ributes", "e cient", "estion": keyword search cannot find "attributes" | The control character is kept as a placeholder; once the whole document is read, each code is resolved by experiment: the candidate letters that turn the most broken words into dictionary words win (`rag.text.ligatures`). A code nothing explains is dropped between letters, never guessed. A repeated author name cannot outvote a real word, and a code with no letters around it is never a ligature. |
| **Words run together** in tightly set type | "speechrecognition", "KimandChoi(2018)" | Table and table-page text is read with a word gap measured against the font size (15%), not a fixed 3 points |
| **Tables that continue over several pages** without repeating the heading | Rows labelled with the previous row's values: "Amodei (2016): Liang (2016) \| Speech: CV" | The heading of the table on the previous page is put on top (`rag.providers.parsers.table_flow`): it starts at the top of its page, the previous one ended at the bottom, same columns and width, and its first row is not a copy of the heading. A first row of plain labels under numeric rows is a new table and is left alone. Rows then read "Paper: Liang et al. (2016) \| Domain: CV \| Tasks: speech recognition" |
| **Captions separated from their tables** | A page of "Table 10 caption, table, Table 11 caption, table" stored as all captions first and all tables after, so each caption sat on the wrong table or none, and a question about Table 11 found nothing | Each table's marker is placed at its height among the page's text lines (`table_flow.interleave`), so a caption stays above its table and becomes that table's section title; a caption wrapped onto a lowercase continuation line is joined |
| **Symbol-font bullets and bracket pieces** (private-use characters) | " Functional impact" repeated 300 times; "  x2 y2" in formulas | A bullet glyph at the start of a line becomes "•"; the pieces of tall brackets are dropped |
| **Line-end hyphens and soft hyphens** | "perfor- mance", "ob\x02tain" | Rejoined when both halves are word parts |
| **Running headers, footers, headings, title** | Page banners inside every chunk | Typography (font size and weight of each line) separates furniture from headings; headings become section boundaries |

Measured effect of the table repairs on the evaluation library: first-rank page for table questions 72.7% -> 81.8%, all
questions 95.0% -> 95.5% (page@3 98.7%, MRR 0.972), and 31 generated column-aware questions (`eval/gen_table_golden.py`,
e.g. "What are the Domain and Method for Weinshall et al. (2018)?") are answered with the right page first 31 times out of 31.

Not covered yet: Arabic scans. The bundled RapidOCR models read English and Chinese; reading Arabic needs the newer
`rapidocr` package with its Arabic recognition model (a download), or Tesseract with the Arabic language data. Arabic PDFs
with a text layer are read normally.

## Measured: five document kinds, twenty questions

Method: five generated files with known answers — (1) a digital prose PDF, (2) a scanned prose PDF (skewed, noise,
JPEG), (3) a PDF with native tables, (4) a PDF whose table is a picture, (5) a photographed inspection form (perspective,
uneven light, blur). Twenty questions, four per file. Each parser's text was cut into chunks of about 500 characters
and searched with the same hybrid (BM25 plus `all-MiniLM-L6-v2`, max-normalised 0.4/0.6). A question counts as a hit
when the chunk holding all its answer facts is in the first 5 results.

| Parser | digital | scanned | native tables | table as picture | photo | all files |
|---|---|---|---|---|---|---|
| **This system** (pdfium + pdfplumber tables + RapidOCR, after the picture fix) | 100% | 100% | 100% | 100% | 100% | **100%** |
| Same, before the fix | 100% | 100% | 100% | **0%** | 100% | 80% |
| Tesseract on every page image | 100% | 100% | 100% | 75% | 100% | 95% |
| PyMuPDF4LLM (Tesseract for OCR) | 100% | 100% | 100% | 0% | no image support | 60% |
| pdfplumber (text layer only) | 100% | 0% | 100% | 0% | 0% | 40% |

Numbers are hit@5. The first-rank rate (hit@1) was 80–85% for the top systems and is limited by the small embedder in
this test, not by parsing. The same 20 questions run as a regression test against the real pipeline
(`tests/integration/test_document_kinds.py`).

What the measurement found and fixed: a table pasted into a text PDF as a picture had **no text at all** in the first
version, because OCR ran only on pages that were mostly picture. Pictures covering at least 4% of a page are now OCR'd.

Caveats, stated plainly:

- The files are synthetic and the fonts are clean. Tesseract does relatively well on clean rendering; the earlier
  testing report found RapidOCR stronger on real skewed, noisy scans. Replace these with your own scans before
  trusting the ranking between OCR engines.
- Twenty questions are a smoke test, not a benchmark. One question is 5 points.
- Word recall and table-cell fidelity were not scored here, only whether the answer chunk was retrievable.

## Docling, MinerU and other layout models

**Docling was not run in this build.** The installation was attempted in the build sandbox but stalled while downloading
its PyTorch dependencies, and Docling's layout and table models are hosted on Hugging Face, which the sandbox blocks
(HTTP 403 confirmed). So there is no Docling number in the table above, and none of the claims about it here are from
this build. To test it yourself, install Docling on a machine that can reach Hugging Face, generate the five files with
`rag.evaluation.bench_corpus.build(folder)`, convert each with Docling, and score the page text against the questions
the same way (a chunk holding all the answer facts within the first five results).

The earlier report (`RAG_retrieval_testing_report.md`) measured Docling, MinerU, PyMuPDF4LLM, Unstructured and the old
repo's parser on real PDFs. Its conclusion applies here: no layout model got every table right, none warned when it was
wrong, and the heavy ones (Docling, MinerU) took 30–100 s for a 11–17 page PDF on CPU. That is the reason they are not
the default for a 5 TB library, where most pages have a good text layer and need none of that machinery.

How to use one anyway: the `PARSER` role is a plugin point. A Docling or MinerU parser is one module that returns
`ParsedDocument` (pages, text, tables), registered with `registry.register(Capability.PARSER, "docling", ...)`, and
selected with `PARSER=docling/default`. Use it for the pages that need layout (complex tables, multi-column scans) and
keep pdfium for the rest; the page router already has the hook (`PdfiumParser._finish_page`). Cloud options already
implemented: `azure_di/prebuilt-layout` and `llamaparse/*`.

## Recommendation per function

| Function | Choice | Why |
|---|---|---|
| PDF with text layer | pdfium + pdfplumber tables | fastest (milliseconds a page), exact text, keeps rows together |
| Scanned pages and pictures | RapidOCR locally; Azure Document Intelligence `prebuilt-layout` when tables in scans matter and cloud is allowed | RapidOCR is free, CPU-only and fast; layout-aware cloud OCR is the upgrade for scanned tables |
| Complex tables | Azure DI / Docling on the pages flagged as table-heavy | none of the local tools is reliable on merged cells |
| Drawings | title block region plus a vision model | text extraction of linework is meaningless |
| Embeddings | `multilingual-e5-small` default, `bge-m3` for Arabic and English quality | see the earlier report: BGE-M3 and Arctic L v2 led, small English-only models scored 0% on Arabic questions |
| Keyword leg | BM25 with an English analyser (own implementation) | the report found engine choice mattered less than stemming |
| Reranker | `ms-marco-MiniLM-L-6-v2` (English), `bge-reranker-v2-m3` (multilingual) | |
