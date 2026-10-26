"use client";

import { Cloud, Cpu } from "lucide-react";

import { ErrorNote, Spinner } from "@/components/ui/feedback";
import { useApi } from "@/lib/hooks";
import { ROLE_NAMES } from "@/lib/roles";

type Call = {
  role: string;
  provider: string;
  model: string;
  calls: number;
  errors: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  masked_values: number;
  cloud: boolean;
  note?: string;
};
type Processing = {
  format: string;
  size: number;
  reader: string;
  read_at: string | null;
  error: string | null;
  warnings: string[];
  methods: { method: string; label: string; pages: number }[];
  pages: { page: number; method: string; label: string | null; drawing: boolean; chars: number; tables: number; engines: string | null; ocr_confidence: number | null }[];
  page_count: number;
  chunks: number;
  embedder: string;
  calls: Call[];
  cloud_used: boolean;
  cost_usd: number;
};

function roleLabel(role: string): string {
  return ROLE_NAMES[role] ?? role;
}

function money(usd: number): string {
  if (!usd) return "free";
  return usd < 0.01 ? `$${usd.toFixed(4)}` : `$${usd.toFixed(3)}`;
}

/** What read this file: the reader, OCR or vision per page, and every model call made for it. */
export function ProcessingPanel({ documentId, versionId }: { documentId: string; versionId: string }) {
  const { data, error } = useApi<Processing>(`/documents/${documentId}/processing?${new URLSearchParams({ version: versionId })}`);
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <Spinner label="Loading processing details" />;
  const cloud = data.calls.filter((c) => c.cloud);
  const showPages = data.pages.some((p) => p.method !== "native");
  return (
    <div className="space-y-3 text-[12.5px]">
      <p className={`flex items-start gap-2 ${data.cloud_used ? "text-warning" : "text-success"}`}>
        {data.cloud_used ? <Cloud size={14} className="mt-0.5 shrink-0" aria-hidden /> : <Cpu size={14} className="mt-0.5 shrink-0" aria-hidden />}
        <span>
          {data.cloud_used
            ? `Cloud models were used for this file (${cloud.map((c) => roleLabel(c.role)).join(", ")}). Total ${money(data.cost_usd)}.`
            : "Read entirely on this server. No cloud model saw this file."}
        </span>
      </p>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-muted">Reader</dt>
        <dd>{data.reader}</dd>
        <dt className="text-muted">Pages</dt>
        <dd>
          {data.page_count} read, {data.chunks} search chunks
        </dd>
        <dt className="text-muted">Embeddings</dt>
        <dd>{data.embedder}</dd>
        {data.read_at ? (
          <>
            <dt className="text-muted">Read at</dt>
            <dd>{new Date(data.read_at).toLocaleString()}</dd>
          </>
        ) : null}
      </dl>
      {data.methods.length ? (
        <ul className="space-y-1">
          {data.methods.map((m) => (
            <li key={m.method} className="flex justify-between gap-3">
              <span>{m.label}</span>
              <span className="shrink-0 text-muted">
                {m.pages} page{m.pages === 1 ? "" : "s"}
              </span>
            </li>
          ))}
        </ul>
      ) : null}
      {data.calls.length ? (
        <div>
          <div className="mb-1 text-[12px] font-medium text-muted">Models used</div>
          <ul className="divide-y divide-border rounded-lg border border-border">
            {data.calls.map((c) => (
              <li key={`${c.role}-${c.model}`} className="px-3 py-2">
                <div className="flex items-center justify-between gap-2">
                  <span className="font-medium">{roleLabel(c.role)}</span>
                  <span className={c.cloud ? "text-warning" : "text-muted"}>{c.cloud ? "cloud" : "local"}</span>
                </div>
                <div className="break-all text-muted">
                  {c.provider}/{c.model}
                </div>
                {c.note ? <div className="text-muted">{c.note}</div> : null}
                <div className="text-muted">
                  {c.calls} call{c.calls === 1 ? "" : "s"}
                  {c.tokens_in + c.tokens_out ? ` · ${c.tokens_in.toLocaleString()} in / ${c.tokens_out.toLocaleString()} out` : ""}
                  {c.cloud ? ` · ${money(c.cost_usd)}` : ""}
                  {c.masked_values ? ` · ${c.masked_values} sensitive value${c.masked_values === 1 ? "" : "s"} masked` : ""}
                  {c.errors ? ` · ${c.errors} failed` : ""}
                </div>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-muted">No model calls are recorded for this file (they are kept for a limited time).</p>
      )}
      {showPages ? (
        <details>
          <summary className="cursor-pointer text-[12px] font-medium text-muted">Page by page</summary>
          <ul className="mt-1.5 space-y-1">
            {data.pages.map((p) => (
              <li key={p.page} className="flex justify-between gap-3">
                <span>
                  p. {p.page}
                  {p.drawing ? " (drawing)" : ""}
                </span>
                <span className="min-w-0 truncate text-right text-muted" title={p.engines ?? p.method}>
                  {p.method}
                  {p.engines ? ` · ${p.engines.replace("vision:openrouter/", "vision ")}` : ""}
                  {p.ocr_confidence ? ` · ${Math.round(p.ocr_confidence * 100)}%` : ""}
                </span>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
      {data.error ? <p className="text-danger">Could not be read: {data.error}</p> : null}
      {data.warnings.length ? (
        <ul className="list-disc space-y-0.5 pl-4 text-warning">
          {data.warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
