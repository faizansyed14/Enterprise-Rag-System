import type { Citation } from "@/lib/types";

/** The original file exactly as uploaded, opened by the browser at the page the answer came from (`#page=N` is understood
 *  by the built-in PDF viewers; `search` makes viewers that support it highlight the first words of the quoted passage). */
export function originalUrl(c: Pick<Citation, "document_id" | "version_id" | "page" | "quote">): string {
  const base = `/api/v1/documents/${c.document_id}/file?${new URLSearchParams({ version: c.version_id })}`;
  const search = c.quote ? `&search=${encodeURIComponent(c.quote.split(/\s+/).slice(0, 8).join(" "))}` : "";
  return `${base}#page=${c.page}${search}`;
}
