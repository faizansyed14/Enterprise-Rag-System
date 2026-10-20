"""Document layout analysis: outline, title and section structure of a PDF from its typography.

The engine in this package (character-level text parse, columns, blocks, running headers and footers, title and heading
detection, outline assembly with embedded bookmarks) was brought over from the earlier code base. It is CPU heavy
(roughly 0.2-0.5 s a page), so it runs in a separate process and only for documents long enough to have structure
(`rag.providers.parsers.layout.adapter.analyse`). Its own text is not used: some glyphs (ligatures) come out wrong in
its character parse, so the page text always comes from pdfium and this package only says where the sections are.

Provenance: the engine appears to descend from the open-source PageIndex project; keep its licence and notice with it
(see NOTICE in the repository root).
"""

from rag.providers.parsers.layout.adapter import LayoutOutline, OutlineEntry, analyse

__all__ = ["LayoutOutline", "OutlineEntry", "analyse"]
