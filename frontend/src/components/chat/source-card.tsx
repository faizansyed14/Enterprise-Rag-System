"use client";

import clsx from "clsx";
import { BadgeCheck, CircleAlert, PanelRight } from "lucide-react";

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
  const name = c.doc_number || c.file_name || c.title;
  const subtitle = [c.title && c.title !== name ? c.title : "", c.project].filter(Boolean).join(" · ");
  return (
    <button
      type="button"
      onClick={() => onSelect(c, display)}
      aria-pressed={active}
      aria-label={`Preview source ${display}: ${name}, page ${pageLabel(c)}${c.verified ? ", verified" : ", not verified"}`}
      className={clsx(
        "group flex w-full items-start gap-3 rounded-xl border px-3 py-2.5 text-left transition-colors duration-150",
        active
          ? "border-foreground/45 bg-surface-hover shadow-soft"
          : "border-border bg-surface-raised hover:border-border-strong hover:bg-surface-hover/50",
      )}
    >
      <span className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border text-[12px] font-semibold tabular">
        {display}
      </span>
      <span className="min-w-0 flex-1">
        <span className="flex items-start gap-2">
          <span className="min-w-0 flex-1">
            <span className="block truncate text-[13px] font-semibold tabular">{name}</span>
            <span className="mt-0.5 block truncate text-[12.5px] text-muted">
              Page <span className="tabular">{pageLabel(c)}</span>
              {subtitle ? ` · ${subtitle}` : ""}
            </span>
          </span>
          <span
            className={clsx(
              "inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-medium",
              c.verified
                ? "border-success/35 bg-success-soft text-success"
                : "border-warning/35 bg-warning-soft text-warning",
            )}
          >
            {c.verified ? <BadgeCheck size={12} aria-hidden /> : <CircleAlert size={12} aria-hidden />}
            {c.verified ? "Verified" : "Unverified"}
          </span>
        </span>
        {c.quote ? (
          <span className="source-text mt-1.5 line-clamp-2 block text-[13.5px] leading-snug text-foreground/85">{c.quote}</span>
        ) : null}
      </span>
      <PanelRight
        size={14}
        className={clsx("mt-1 shrink-0 text-muted", active ? "text-foreground" : "group-hover:text-foreground")}
        aria-hidden
      />
    </button>
  );
}
