"use client";

import clsx from "clsx";

import { ModelIcon } from "@/components/brand/brand";
import { ErrorNote, Spinner, StatusBadge } from "@/components/ui/feedback";
import { Drawer } from "@/components/ui/overlay";
import { ms, relativeTime, usd } from "@/lib/format";
import { useApi } from "@/lib/hooks";

type Span = {
  id: string;
  parent_id: string | null;
  started_at: string;
  name: string;
  kind: string;
  role: string | null;
  provider: string | null;
  model: string | null;
  duration_ms: number;
  status: string;
  error: string | null;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number | null;
  attrs: Record<string, unknown>;
};
type Trace = { id: string; started_at: string; kind: string; name: string; status: string; duration_ms: number | null; attrs: Record<string, unknown> };

const MODEL_KINDS = new Set(["chat", "vision", "embedder", "reranker", "groundedness", "ocr", "parser"]);

/** One request as a waterfall: each stage and model call on a shared time axis. */
export function TraceDrawer({ traceId, onClose }: { traceId: string; onClose: () => void }) {
  const { data, error } = useApi<{ trace: Trace; spans: Span[] }>(`/monitor/traces/${traceId}`);
  const start = data ? new Date(data.trace.started_at).getTime() : 0;
  const total = data ? Math.max(data.trace.duration_ms ?? 0, ...data.spans.map((s) => new Date(s.started_at).getTime() - start + s.duration_ms), 1) : 1;

  // Depth from parent links, so nested stages are indented under their parent.
  const depth = new Map<string, number>();
  for (const s of data?.spans ?? []) depth.set(s.id, s.parent_id && depth.has(s.parent_id) ? (depth.get(s.parent_id) ?? 0) + 1 : 0);

  return (
    <Drawer
      open
      onClose={onClose}
      width="min(760px, 100vw)"
      title={data ? (data.trace.kind === "chat" ? "Question" : `${data.trace.kind}: ${data.trace.name}`) : "Trace"}
      subtitle={data ? `${relativeTime(data.trace.started_at)}, ${ms(data.trace.duration_ms)} in total` : traceId}
    >
      <div className="px-5 py-4">
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading the trace" /> : null}
        {data ? (
          <>
            <div className="mb-4 flex flex-wrap items-center gap-2 text-[12.5px]">
              {data.trace.status === "ok" ? <StatusBadge state="ok">Completed</StatusBadge> : <StatusBadge state="fail">{data.trace.status}</StatusBadge>}
              {Object.entries(data.trace.attrs || {})
                .filter(([k]) => ["status", "model", "conversation", "lane", "job_id"].includes(k))
                .map(([k, v]) => (
                  <span key={k} className="rounded-md border border-border px-1.5 py-px text-muted">
                    {k}: <span className="text-foreground tabular">{String(v)}</span>
                  </span>
                ))}
            </div>
            <div className="space-y-px">
              {data.spans.map((s) => {
                const offset = ((new Date(s.started_at).getTime() - start) / total) * 100;
                const widthPct = Math.max(0.6, (s.duration_ms / total) * 100);
                const model = MODEL_KINDS.has(s.kind) && s.provider && s.model;
                return (
                  <div key={s.id} className="grid grid-cols-[minmax(0,240px)_minmax(0,1fr)_64px] items-center gap-3 rounded-md px-1 py-1 text-[12.5px] hover:bg-surface-hover">
                    <div className="flex min-w-0 items-center gap-1.5" style={{ paddingLeft: (depth.get(s.id) ?? 0) * 12 }}>
                      {model ? <ModelIcon provider={s.provider as string} model={s.model as string} size={14} tile={false} /> : null}
                      <span className={clsx("truncate", s.status !== "ok" && "text-danger")} title={s.error || `${s.name}${s.model ? ` (${s.provider}/${s.model})` : ""}`}>
                        {s.name}
                      </span>
                    </div>
                    <div className="relative h-3.5 rounded-sm bg-surface">
                      <span
                        className={clsx("absolute inset-y-0 rounded-sm", s.status === "ok" ? "bg-foreground/75" : "bg-danger")}
                        style={{ left: `${Math.min(offset, 99.4)}%`, width: `${Math.min(widthPct, 100 - Math.min(offset, 99.4))}%` }}
                      />
                    </div>
                    <span className="text-right text-muted tabular">{ms(s.duration_ms)}</span>
                  </div>
                );
              })}
            </div>
            <table className="mt-6 w-full text-[12.5px]">
              <thead>
                <tr className="text-left text-muted">
                  <th className="pb-1.5 font-medium">Model call</th>
                  <th className="pb-1.5 text-right font-medium">Tokens in</th>
                  <th className="pb-1.5 text-right font-medium">Tokens out</th>
                  <th className="pb-1.5 text-right font-medium">Cost</th>
                </tr>
              </thead>
              <tbody>
                {data.spans
                  .filter((s) => MODEL_KINDS.has(s.kind))
                  .map((s) => (
                    <tr key={s.id} className="border-t border-border/70">
                      <td className="py-1.5">
                        <div>{s.name}</div>
                        <div className="text-[11.5px] text-muted tabular">
                          {s.provider}/{s.model}
                        </div>
                        {s.error ? <div className="text-[11.5px] text-danger">{s.error}</div> : null}
                      </td>
                      <td className="py-1.5 text-right tabular">{s.tokens_in || "—"}</td>
                      <td className="py-1.5 text-right tabular">{s.tokens_out || "—"}</td>
                      <td className="py-1.5 text-right tabular">{s.cost_usd ? usd(s.cost_usd) : "—"}</td>
                    </tr>
                  ))}
              </tbody>
            </table>
          </>
        ) : null}
      </div>
    </Drawer>
  );
}
