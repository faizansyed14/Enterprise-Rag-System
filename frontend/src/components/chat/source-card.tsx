"use client";

import clsx from "clsx";
import { PanelRight } from "lucide-react";

import type { Citation } from "@/lib/types";

export function pageLabel(c: Pick<Citation, "page" | "page_end">): string {
  return !c.page_end || c.page_end === c.page ? `${c.page}` : `${c.page}–${c.page_end}`;
}

/** A cited source: the file and the page it was found on. Clicking previews it beside the chat. */
export function SourceCard({
  citation,
  display,
  active,
  onSelect,
}: {
  citation: Citation;
  display: number;
  active?: boolean;
  onSelect: (citation: Citation, display: number) => void;
}) {
  const c = citation;
  const name = c.file_name || c.doc_number || c.title;
  const subtitle = c.title && c.title !== c.file_name ? c.title : "";
  return (
    <button
      type="button"
      onClick={() => onSelect(c, display)}
      aria-pressed={active}
      aria-label={`Preview ${name}, page ${pageLabel(c)}`}
      className={clsx(
        "group flex w-full items-center gap-3 rounded-xl border bg-surface-raised px-3 py-2.5 text-left transition-colors",
        active ? "border-foreground/40 bg-surface-hover" : "border-border hover:border-border-strong",
      )}
    >
      <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border text-[12px] font-semibold tabular">{display}</span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] font-semibold tabular">{name}</span>
        <span className="block truncate text-[12.5px] text-muted">
          Page {pageLabel(c)}
          {subtitle ? ` · ${subtitle}` : ""}
          {c.project ? ` · ${c.project}` : ""}
        </span>
      </span>
      <span className="flex shrink-0 items-center gap-1.5">
        <PanelRight size={14} className={clsx("text-muted", active ? "text-foreground" : "group-hover:text-foreground")} aria-hidden />
      </span>
    </button>
  );
}
