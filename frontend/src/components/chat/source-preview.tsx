"use client";

import { BadgeCheck, CircleAlert, ExternalLink, X } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent, type PointerEvent as ReactPointerEvent, type RefObject } from "react";

import { PageViewer } from "@/components/documents/page-viewer";
import { Spinner } from "@/components/ui/feedback";
import { useApi, useEscape } from "@/lib/hooks";
import type { Citation, DocumentDetail } from "@/lib/types";

import { pageLabel } from "./source-card";

export type SourcePreview = { citation: Citation; display: number };

function PreviewBody({ citation }: { citation: Citation }) {
  const c = citation;
  const { data: doc, loading } = useApi<DocumentDetail>(`/documents/${c.document_id}`);
  const version = useMemo(
    () => doc?.versions.find((v) => v.id === c.version_id) ?? doc?.versions[0],
    [doc, c.version_id],
  );
  const pageCount = Math.max(version?.page_count ?? c.page_end ?? c.page, c.page, 1);

  if (loading && !doc) {
    return (
      <div className="flex h-full items-center justify-center p-6">
        <Spinner label="Opening source" />
      </div>
    );
  }

  return (
    <PageViewer
      mode="raw"
      documentId={c.document_id}
      versionId={c.version_id}
      pageCount={pageCount}
      page={c.page}
      quote={c.quote || undefined}
      format={version?.format}
    />
  );
}

function PaneChrome({
  preview,
  onClose,
  className,
  closeRef,
}: {
  preview: SourcePreview;
  onClose: () => void;
  className?: string;
  closeRef?: RefObject<HTMLButtonElement | null>;
}) {
  const c = preview.citation;
  const title = c.doc_number || c.file_name || c.title;
  const modal = Boolean(className?.includes("fixed"));
  return (
    <aside className={className} aria-label={`Source ${preview.display} preview`} role={modal ? "dialog" : undefined} aria-modal={modal || undefined}>
      <header className="flex shrink-0 flex-col gap-1.5 border-b border-border px-4 py-2.5">
        <div className="flex items-center gap-2">
          <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md border border-border text-[12px] font-semibold tabular">
            {preview.display}
          </span>
          <div className="min-w-0 flex-1 truncate text-[14px] font-semibold tabular">{title}</div>
          <Link
            href={`/documents/${c.document_id}`}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg px-2 text-[12.5px] text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
          >
            <ExternalLink size={13} aria-hidden />
            <span className="hidden sm:inline">Open original</span>
          </Link>
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            aria-label="Close source preview"
            title="Close source preview"
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
          >
            <X size={16} aria-hidden />
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 pl-8 text-[12px] text-muted">
          <span className="tabular">Page {pageLabel(c)}</span>
          <span
            className={
              c.verified
                ? "inline-flex items-center gap-1 text-success"
                : "inline-flex items-center gap-1 text-warning"
            }
          >
            {c.verified ? <BadgeCheck size={12} aria-hidden /> : <CircleAlert size={12} aria-hidden />}
            {c.verified ? "Verified quote" : "Unverified quote"}
          </span>
        </div>
      </header>
      <div className="min-h-0 flex-1">
        <PreviewBody
          key={`${c.document_id}:${c.version_id}:${c.page}:${c.n}`}
          citation={c}
        />
      </div>
    </aside>
  );
}

const PANE_KEY = "rag-source-pane";

/** Right-hand raw file preview beside chat. Drag the edge to resize against the chat. */
export function SourcePreviewPane({
  preview,
  onClose,
}: {
  preview: SourcePreview;
  onClose: () => void;
}) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const previousFocus = useRef<HTMLElement | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(46);

  useEscape(onClose, true);

  useEffect(() => {
    const saved = Number(sessionStorage.getItem(PANE_KEY));
    if (saved >= 22 && saved <= 78) setWidth(saved);
  }, []);

  const setWidthPct = useCallback((value: number) => {
    const next = Math.min(78, Math.max(22, value));
    setWidth(next);
    sessionStorage.setItem(PANE_KEY, String(Math.round(next)));
  }, []);

  const onSplitPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLDivElement>) => {
      event.preventDefault();
      const row = rowRef.current?.parentElement;
      if (!row) return;
      const move = (ev: PointerEvent) => {
        const rect = row.getBoundingClientRect();
        if (rect.width <= 0) return;
        setWidthPct(((rect.right - ev.clientX) / rect.width) * 100);
      };
      const stop = () => {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", stop);
      };
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", stop);
    },
    [setWidthPct],
  );

  const onSplitKey = useCallback(
    (event: KeyboardEvent<HTMLDivElement>) => {
      const step = event.shiftKey ? 10 : 4;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        setWidthPct(width + step);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        setWidthPct(width - step);
      }
    },
    [setWidthPct, width],
  );

  useEffect(() => {
    previousFocus.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeRef.current?.focus();
    return () => {
      previousFocus.current?.focus?.();
    };
  }, [preview.citation.n, preview.citation.document_id, preview.citation.page]);

  return (
    <>
      <div ref={rowRef} className="hidden h-full min-h-0 lg:flex" style={{ flex: `0 0 ${width}%` }}>
        <div
          role="separator"
          aria-orientation="vertical"
          aria-valuemin={22}
          aria-valuemax={78}
          aria-valuenow={Math.round(width)}
          aria-label="Resize chat and source"
          tabIndex={0}
          onPointerDown={onSplitPointerDown}
          onKeyDown={onSplitKey}
          className="group relative z-10 flex w-2 shrink-0 touch-none cursor-col-resize items-center justify-center bg-border outline-none focus-visible:bg-foreground/40"
        >
          <span className="h-8 w-0.5 rounded-full bg-foreground/35 group-hover:bg-foreground/70" />
        </div>
        <PaneChrome
          preview={preview}
          onClose={onClose}
          closeRef={closeRef}
          className="flex h-full min-h-0 min-w-0 flex-1 flex-col bg-background"
        />
      </div>
      <div className="lg:hidden">
        <div className="fixed inset-0 z-30 bg-black/30" onClick={onClose} aria-hidden />
        <PaneChrome
          preview={preview}
          onClose={onClose}
          className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[560px] flex-col border-l border-border bg-background shadow-panel"
        />
      </div>
    </>
  );
}
