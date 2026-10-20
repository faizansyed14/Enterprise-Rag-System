"use client";

import { X } from "lucide-react";
import { useMemo } from "react";

import { PageViewer } from "@/components/documents/page-viewer";
import { IconButton } from "@/components/ui/button";
import { Spinner } from "@/components/ui/feedback";
import { useApi, useEscape } from "@/lib/hooks";
import type { Citation, DocumentDetail } from "@/lib/types";

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
  title,
  onClose,
  citation,
  className,
}: {
  title: string;
  onClose: () => void;
  citation: Citation;
  className?: string;
}) {
  return (
    <aside className={className} aria-label="Source preview">
      <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-2.5">
        <div className="min-w-0 flex-1 truncate text-[14px] font-semibold tabular">{title}</div>
        <IconButton label="Close source preview" onClick={onClose}>
          <X size={16} />
        </IconButton>
      </header>
      <div className="min-h-0 flex-1">
        <PreviewBody
          key={`${citation.document_id}:${citation.version_id}:${citation.page}:${citation.n}`}
          citation={citation}
        />
      </div>
    </aside>
  );
}

/** Right-hand raw file preview beside chat (zoomable; no extract pane). */
export function SourcePreviewPane({
  preview,
  onClose,
}: {
  preview: SourcePreview;
  onClose: () => void;
}) {
  const c = preview.citation;
  const title = c.doc_number || c.file_name || c.title;

  useEscape(onClose, true);

  return (
    <>
      <PaneChrome
        title={title}
        onClose={onClose}
        citation={c}
        className="hidden h-full min-h-0 w-[min(540px,46vw)] shrink-0 flex-col border-l border-border bg-background lg:flex"
      />
      <div className="lg:hidden">
        <div className="fixed inset-0 z-30 bg-black/30" onClick={onClose} aria-hidden />
        <PaneChrome
          title={title}
          onClose={onClose}
          citation={c}
          className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[560px] flex-col border-l border-border bg-background shadow-panel"
        />
      </div>
    </>
  );
}
