"use client";

import clsx from "clsx";
import { ArrowLeft, Download, PanelRight, Trash2 } from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type ReactNode } from "react";

import { PageViewer } from "@/components/documents/page-viewer";
import { PrivacyBanner } from "@/components/documents/privacy";
import { ProcessingPanel } from "@/components/documents/processing";
import { Button, IconButton } from "@/components/ui/button";
import { ErrorNote, Spinner } from "@/components/ui/feedback";
import { api, errorMessage } from "@/lib/api";
import { docTypeLabel, pct, shortDate, titleCase } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { DocumentDetail, VersionInfo } from "@/lib/types";

// Page reading methods recorded at extraction ("text_layer:12,ocr:3": method and page count).
const METHODS: Record<string, string> = {
  text_layer: "PDF text layer",
  native: "File contents",
  ocr: "OCR",
  vision: "Vision model",
  cloud: "Cloud parser",
  empty: "No text",
};

function extractMethod(value: string | null): string {
  if (!value) return "—";
  const parts = value.split(",").map((part) => {
    const [method, count] = part.split(":");
    const name = METHODS[method] ?? titleCase(method);
    return count && value.includes(",") ? `${name} (${count} pages)` : name;
  });
  return parts.join(", ");
}

function Panel({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="border-b border-border px-5 py-4">
      <h2 className="mb-2.5 text-[13px] font-semibold">{title}</h2>
      {children}
    </section>
  );
}

/** The document's particulars as a drawing title block. */
function TitleBlock({ doc, version }: { doc: DocumentDetail; version: VersionInfo }) {
  const cells: [string, ReactNode][] = [
    ["Document number", doc.doc_number || "—"],
    ["Project", doc.project_ids.join(", ") || "—"],
    ["Type", docTypeLabel(doc.doc_type)],
    ["Specification section", doc.spec_section || "—"],
    ["Pages", version.page_count],
    ["Language", version.language ? version.language.toUpperCase() : "—"],
  ];
  return (
    <div className="grid grid-cols-2 border-l border-t border-border text-[12.5px]">
      {cells.map(([label, value]) => (
        <div key={label} className="min-w-0 border-b border-r border-border px-2.5 py-1.5">
          <div className="text-[10.5px] leading-4 text-muted">{label}</div>
          <div className="truncate tabular">{value}</div>
        </div>
      ))}
    </div>
  );
}

function Detail() {
  const { id } = useParams<{ id: string }>();
  const params = useSearchParams();
  const router = useRouter();
  const pathname = usePathname();
  const { data: doc, error, loading } = useApi<DocumentDetail>(`/documents/${id}`);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState("");
  const [showDetails, setShowDetails] = useState(false);

  const versionId = params.get("version");
  const page = Math.max(1, Number(params.get("page") || 1) || 1);
  const quote = params.get("q") || undefined;
  const version = doc?.versions.find((v) => v.id === versionId) ?? doc?.versions[0];

  useEffect(() => {
    if (doc) document.title = doc.doc_number || doc.title;
  }, [doc]);

  const go = (next: { version?: string; page?: number; keepQuote?: boolean }) => {
    const q = new URLSearchParams();
    const v = next.version ?? version?.id;
    if (v) q.set("version", v);
    q.set("page", String(next.page ?? page));
    if (next.keepQuote && quote) q.set("q", quote);
    router.replace(`${pathname}?${q}`, { scroll: false });
  };

  const onDelete = async () => {
    if (!doc) return;
    setDeleting(true);
    setDeleteError("");
    try {
      await api(`/documents/${doc.id}`, { method: "DELETE" });
      router.push("/documents");
    } catch (err) {
      setDeleteError(errorMessage(err));
      setDeleting(false);
    }
  };

  if (error) {
    return (
      <div className="px-6 py-8 lg:px-10">
        <ErrorNote>{error === "Document not found" ? "This document does not exist or you do not have access to it." : error}</ErrorNote>
      </div>
    );
  }
  if (loading && !doc) {
    return (
      <div className="px-6 py-8 lg:px-10">
        <Spinner label="Opening the document" />
      </div>
    );
  }
  if (!doc || !version) return null;

  const metadata = Object.entries(version.metadata || {}).filter(([, m]) => m && m.value !== null && m.value !== "");

  const originalHref = `/api/v1/documents/${doc.id}/file?${new URLSearchParams({ version: version.id })}#page=${page}`;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <header className="shrink-0 border-b border-border px-4 py-3 sm:px-6 lg:px-8">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
          <Link href="/documents" className="inline-flex items-center gap-1.5 text-[12.5px] text-muted hover:text-foreground">
            <ArrowLeft size={14} aria-hidden />
            Documents
          </Link>
          <span className="hidden text-border-strong sm:inline" aria-hidden>
            /
          </span>
          <div className="min-w-0 flex-1">
            <h1 className="truncate text-[18px] font-semibold leading-tight tracking-[-0.01em] tabular sm:text-[20px]">
              {doc.doc_number || version.file_name || doc.title}
            </h1>
            <p dir="auto" className="source-text truncate text-[14px] text-muted sm:text-[15px]">
              {doc.doc_number ? doc.title : version.path || doc.title}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-1.5">
            <IconButton
              label={showDetails ? "Hide details" : "Show details"}
              onClick={() => setShowDetails((v) => !v)}
              className={clsx(showDetails && "bg-surface-hover text-foreground")}
            >
              <PanelRight size={15} />
            </IconButton>
            <a
              href={`/api/v1/documents/${doc.id}/download?version=${version.id}`}
              download
              className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border-strong px-2.5 text-[13px] font-medium hover:bg-surface-hover"
            >
              <Download size={14} aria-hidden />
              Download
            </a>
            <Button
              size="sm"
              variant="ghost"
              icon={<Trash2 size={14} aria-hidden />}
              onClick={() => setConfirmDelete(true)}
              aria-label="Delete document"
            >
              Delete
            </Button>
          </div>
        </div>
        {confirmDelete ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-danger/40 bg-danger-soft px-3 py-2 text-[12.5px] text-danger">
            Delete this document? The file, search index entries, and derived data are removed permanently.
            <Button size="sm" variant="danger" disabled={deleting} onClick={() => void onDelete()}>
              {deleting ? "Deleting…" : "Delete"}
            </Button>
            <Button size="sm" variant="ghost" disabled={deleting} onClick={() => setConfirmDelete(false)}>
              Keep
            </Button>
            {deleteError ? <span className="w-full text-danger">{deleteError}</span> : null}
          </div>
        ) : null}
      </header>
      <PrivacyBanner documentId={doc.id} versionId={version.id} />
      <div className="flex min-h-0 flex-1">
        <div className="min-w-0 flex-1">
          <PageViewer
            documentId={doc.id}
            versionId={version.id}
            pageCount={version.page_count}
            page={page}
            quote={quote}
            format={version.format}
            originalHref={originalHref}
            onPageChange={(n) => go({ page: n, keepQuote: n === page })}
          />
        </div>
        {showDetails ? (
          <aside className="w-full max-w-full shrink-0 overflow-y-auto border-l border-border bg-background sm:w-[340px] xl:w-[380px]">
            <Panel title="Title block">
              <TitleBlock doc={doc} version={version} />
            </Panel>
            <Panel title="How it was read">
              <ProcessingPanel documentId={doc.id} versionId={version.id} />
            </Panel>
            {doc.references.length ? (
              <Panel title="Refers to">
                <ul className="space-y-1.5 text-[12.5px]">
                  {doc.references.slice(0, 30).map((r, i) => (
                    <li key={`${r.target}-${i}`} className="flex items-baseline gap-2">
                      {r.target_document_id ? (
                        <Link href={`/documents/${r.target_document_id}`} className="font-medium tabular underline-offset-2 hover:underline">
                          {r.target}
                        </Link>
                      ) : (
                        <span className="font-medium tabular">{r.target}</span>
                      )}
                      <span className="text-muted">{titleCase(r.kind)}</span>
                      {r.page_no ? (
                        <button type="button" onClick={() => go({ page: r.page_no ?? 1 })} className="ml-auto shrink-0 text-muted hover:text-foreground">
                          p. {r.page_no}
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              </Panel>
            ) : null}
            {doc.cited_by.length ? (
              <Panel title="Referred to by">
                <ul className="space-y-1.5 text-[12.5px]">
                  {doc.cited_by.map((c) => (
                    <li key={c.id} className="truncate">
                      <Link href={`/documents/${c.id}`} className="underline-offset-2 hover:underline">
                        <span className="font-medium tabular">{c.doc_number || c.title}</span>
                        {c.doc_number ? <span className="text-muted"> {c.title}</span> : null}
                      </Link>
                    </li>
                  ))}
                </ul>
              </Panel>
            ) : null}
            <Panel title="File and extracted fields">
              <dl className="grid grid-cols-[110px_1fr] gap-x-3 gap-y-1 text-[12.5px]">
                <dt className="text-muted">File</dt>
                <dd className="min-w-0 truncate" title={version.path ?? undefined}>{version.file_name || "—"}</dd>
                <dt className="text-muted">Format</dt>
                <dd className="uppercase">{version.format}</dd>
                <dt className="text-muted">Text from</dt>
                <dd>{extractMethod(version.extract_method)}</dd>
                <dt className="text-muted">Passages</dt>
                <dd className="tabular">{version.chunk_count}</dd>
                {version.live_at ? (
                  <>
                    <dt className="text-muted">Searchable since</dt>
                    <dd>{shortDate(version.live_at)}</dd>
                  </>
                ) : null}
              </dl>
              {version.warnings?.length ? (
                <ul className="mt-2.5 space-y-1 text-[12px] text-warning">
                  {version.warnings.slice(0, 6).map((w, i) => (
                    <li key={i}>{w}</li>
                  ))}
                </ul>
              ) : null}
              {metadata.length ? (
                <table className="mt-3 w-full text-[12px]">
                  <thead>
                    <tr className="text-left text-muted">
                      <th className="pb-1 font-medium">Field</th>
                      <th className="pb-1 font-medium">Value</th>
                      <th className="pb-1 text-right font-medium" title="How sure the reader is, and where the value came from">
                        Confidence
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {metadata.map(([field, m]) => (
                      <tr key={field} className="border-t border-border/70 align-top">
                        <td className="py-1 pr-2 text-muted">{titleCase(field)}</td>
                        <td className="max-w-[150px] truncate py-1 pr-2" title={String(m.value)}>
                          {Array.isArray(m.value) ? m.value.join(", ") : String(m.value)}
                        </td>
                        <td className="py-1 text-right tabular" title={`From ${m.source}`}>
                          {pct(m.confidence)}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
            </Panel>
          </aside>
        ) : null}
      </div>
    </div>
  );
}

export default function DocumentPage() {
  return (
    <Suspense fallback={null}>
      <Detail />
    </Suspense>
  );
}
