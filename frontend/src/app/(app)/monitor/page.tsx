"use client";

import clsx from "clsx";
import { RotateCcw } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useMemo, useState } from "react";

import { BrandIcon, ModelIcon, modelLabel, splitSpec } from "@/components/brand/brand";
import { LineChart, bucketize } from "@/components/charts/line-chart";
import { TraceDrawer } from "@/components/monitor/trace-drawer";
import { Button } from "@/components/ui/button";
import { Badge, ErrorNote, Meter, Spinner, StatusBadge } from "@/components/ui/feedback";
import { GridFillers, PageHeader, Section, Stat, Table, Tabs, Td, Th } from "@/components/ui/layout";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { compact, ms, relativeTime, usd } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { ROLE_NAMES } from "@/lib/roles";

type ModelRow = { kind: string; role: string | null; provider: string | null; model: string | null; calls: number; errors: number; avg_ms: number | null; p50_ms: number | null; p95_ms: number | null; max_ms: number; tokens_in: number; tokens_out: number; cost_usd: number };
type Lane = { queued: number; running: number; dead: number; failed: number; oldest_ready_s: number };
type Queue = { lanes: Record<string, Lane>; kinds: Record<string, Record<string, number>> };
type Process = { id: string; kind: string; host: string; pid: number; started_at: string; age_s: number; online: boolean; stats: { in_flight?: Record<string, number>; totals?: Record<string, number>; current?: Record<string, unknown>; buffered?: number; dropped?: number; rss_mb?: number } };
type Alert = { id: number; rule: string; severity: string; message: string; count: number; first_seen_at: string; last_seen_at: string };
type Failure = { id: number; kind: string; lane: string; status: string; attempts: number; last_error: string; finished_at: string | null };
type ModelError = { started_at: string; role: string | null; provider: string | null; model: string | null; error: string | null; trace_id: string | null };
type CacheFamily = { hits: number; misses: number; hit_rate: number | null };
type CacheStats = { enabled: boolean; available?: boolean; keys?: number; memory?: string; answer?: CacheFamily; embed?: CacheFamily; rerank?: CacheFamily };
type Summary = {
  window_minutes: number;
  chat: { requests: number; errors: number; p50_ms: number; p95_ms: number };
  cost: { today_usd: number; budget_usd: number };
  content: { documents: number; live_versions: number; in_progress: number; failed_versions: number; failed_items: number; chunks: number; pages: number };
  models: ModelRow[];
  processes: Process[];
  queue: Queue;
  alerts: Alert[];
  cache?: CacheStats;
  failures: Failure[];
  model_errors: ModelError[];
};
type Timeseries = { chat: { minute: string; requests: number; errors: number; p95_ms: number }[]; calls: { minute: string; kind: string; calls: number; errors: number; cost_usd: number }[] };
type TraceRow = { id: string; started_at: string; name: string; status: string; duration_ms: number | null; attrs: Record<string, unknown>; email: string | null };
type Check = { name: string; status: string; detail: string };

const WINDOWS = [
  { value: "15", label: "15 min" },
  { value: "60", label: "1 hour" },
  { value: "360", label: "6 hours" },
  { value: "1440", label: "24 hours" },
] as const;

const KIND_NAMES: Record<string, string> = {
  chat: "Chat models",
  embedder: "Embeddings",
  reranker: "Reranking",
  groundedness: "Answer checks",
  ocr: "OCR",
  parser: "PDF reading",
  vision: "Vision",
  api: "API requests",
};

const PROCESS_NAMES: Record<string, string> = { api: "API", worker: "Worker", scheduler: "Scheduler" };

const LANES: Record<string, string> = {
  control: "Permission changes and deletions",
  interactive: "Uploads and manual syncs",
  bulk: "Scheduled syncs and rebuilds",
};

const titleCaseCheck = (name: string) => name.charAt(0).toUpperCase() + name.slice(1);

function useLive(onData: (data: { processes: Process[]; queue: Queue; alerts: number }) => void) {
  const [connected, setConnected] = useState(false);
  useEffect(() => {
    const source = new EventSource("/api/v1/monitor/stream");
    source.addEventListener("live", (event) => {
      setConnected(true);
      try {
        onData(JSON.parse((event as MessageEvent).data));
      } catch {
        /* ignore a malformed frame */
      }
    });
    source.onerror = () => setConnected(false);
    return () => source.close();
  }, [onData]);
  return connected;
}

function ModelCalls({ rows }: { rows: ModelRow[] }) {
  const models = rows.filter((r) => r.kind !== "api");
  const top = Math.max(1, ...models.map((r) => r.calls));
  if (!models.length) return <p className="px-5 py-6 text-[13px] text-muted">No model calls in this window.</p>;
  return (
    <Table>
      <thead>
        <tr>
          <Th>Model</Th>
          <Th>Role</Th>
          <Th align="right">Calls</Th>
          <Th align="right">Errors</Th>
          <Th align="right">p50</Th>
          <Th align="right">p95</Th>
          <Th align="right">Tokens in / out</Th>
          <Th align="right">Cost</Th>
        </tr>
      </thead>
      <tbody>
        {models.map((r, i) => (
          <tr key={`${r.kind}-${r.role}-${r.provider}-${r.model}-${i}`}>
            <Td className="max-w-[300px]">
              <div className="flex items-center gap-2.5">
                {r.provider ? <ModelIcon provider={r.provider} model={r.model ?? ""} size={24} /> : null}
                <div className="min-w-0">
                  <div className="truncate font-medium tabular">{modelLabel(r.provider, r.model) || r.kind}</div>
                  <div className="text-[12px] text-muted">{KIND_NAMES[r.kind] ?? r.kind}</div>
                </div>
              </div>
            </Td>
            <Td className="text-muted">{r.role ?? "—"}</Td>
            <Td align="right" className="min-w-[140px]">
              <div className="flex items-center justify-end gap-2">
                <span className="hidden h-1.5 w-16 overflow-hidden rounded-full bg-surface sm:block">
                  <span className="block h-full rounded-full bg-foreground/70" style={{ width: `${(r.calls / top) * 100}%` }} />
                </span>
                {compact(r.calls)}
              </div>
            </Td>
            <Td align="right" className={r.errors ? "text-danger" : "text-muted"}>
              {r.errors || "0"}
            </Td>
            <Td align="right">{ms(r.p50_ms)}</Td>
            <Td align="right">{ms(r.p95_ms)}</Td>
            <Td align="right" className="text-muted">
              {r.tokens_in || r.tokens_out ? `${compact(r.tokens_in)} / ${compact(r.tokens_out)}` : "—"}
            </Td>
            <Td align="right">{r.cost_usd ? usd(r.cost_usd) : "—"}</Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

/** What the shared cache (Redis) has saved: repeated answers, embeddings and rerank scores. */
function CachePanel({ cache }: { cache: CacheStats | undefined }) {
  if (!cache?.enabled) return <p className="px-5 py-6 text-[13px] text-muted">Redis is not set up: caches stay inside each process.</p>;
  if (!cache.available) return <p className="px-5 py-6 text-[13px] text-warning">Redis is set up but not reachable; the application is using its in-process fallbacks.</p>;
  const rows: [string, CacheFamily | undefined][] = [
    ["Answers (no model call)", cache.answer],
    ["Rerank scores", cache.rerank],
    ["Question embeddings", cache.embed],
  ];
  return (
    <div>
      <Table>
        <thead>
          <tr>
            <Th>Cached</Th>
            <Th align="right">Hits</Th>
            <Th align="right">Misses</Th>
            <Th align="right">Hit rate</Th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([label, f]) => (
            <tr key={label}>
              <Td>{label}</Td>
              <Td align="right">{compact(f?.hits ?? 0)}</Td>
              <Td align="right">{compact(f?.misses ?? 0)}</Td>
              <Td align="right">{f?.hit_rate == null ? "—" : `${Math.round(f.hit_rate * 100)}%`}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
      <p className="px-5 py-3 text-[12px] text-muted">
        {compact(cache.keys ?? 0)} entries · {cache.memory} in memory. An answer is served from the cache only for the same question, the same access and an unchanged library.
      </p>
    </div>
  );
}

function Queues({ queue }: { queue: Queue }) {
  return (
    <Table>
      <thead>
        <tr>
          <Th>Lane</Th>
          <Th align="right">Waiting</Th>
          <Th align="right">Running</Th>
          <Th align="right">Retrying</Th>
          <Th align="right">Dead</Th>
          <Th align="right">Oldest wait</Th>
        </tr>
      </thead>
      <tbody>
        {Object.entries(queue.lanes).map(([lane, l]) => (
          <tr key={lane}>
            <Td>
              <div className="font-medium capitalize">{lane}</div>
              <div className="text-[12px] text-muted">{LANES[lane] ?? ""}</div>
            </Td>
            <Td align="right">{compact(l.queued)}</Td>
            <Td align="right">{compact(l.running)}</Td>
            <Td align="right" className={l.failed ? "text-warning" : "text-muted"}>
              {compact(l.failed)}
            </Td>
            <Td align="right" className={l.dead ? "text-danger" : "text-muted"}>
              {compact(l.dead)}
            </Td>
            <Td align="right" className={l.oldest_ready_s > 600 ? "text-warning" : undefined}>
              {l.oldest_ready_s ? ms(l.oldest_ready_s * 1000) : "—"}
            </Td>
          </tr>
        ))}
      </tbody>
    </Table>
  );
}

function Processes({ processes }: { processes: Process[] }) {
  if (!processes.length) return <p className="px-5 py-6 text-[13px] text-muted">No API or worker processes have reported in.</p>;
  return (
    <Table>
      <thead>
        <tr>
          <Th>Process</Th>
          <Th>State</Th>
          <Th align="right">Busy</Th>
          <Th align="right">Memory</Th>
          <Th align="right">Running for</Th>
        </tr>
      </thead>
      <tbody>
        {processes.map((p) => {
          const busy = Object.values(p.stats?.in_flight ?? {}).reduce((a, b) => a + b, 0);
          return (
            <tr key={p.id}>
              <Td>
                <div className="font-medium">{PROCESS_NAMES[p.kind] ?? p.kind}</div>
                <div className="text-[12px] text-muted tabular">
                  {p.host}, pid {p.pid}
                </div>
              </Td>
              <Td>{p.online ? <StatusBadge state="ok">Online</StatusBadge> : <StatusBadge state="fail">Silent {Math.round(p.age_s)} s</StatusBadge>}</Td>
              <Td align="right">{busy}</Td>
              <Td align="right">{p.stats?.rss_mb ? `${Math.round(p.stats.rss_mb)} MB` : "—"}</Td>
              <Td align="right" className="text-muted">
                {relativeTime(p.started_at).replace(" ago", "")}
              </Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}

function Failures({ failures, onRetried }: { failures: Failure[]; onRetried: () => void }) {
  const toast = useToast();
  const dead = failures.filter((f) => f.status === "dead");
  const retry = async () => {
    try {
      const r = await api<{ retried: number }>("/jobs/retry", { body: { ids: dead.map((f) => f.id) } });
      toast(`${r.retried} jobs queued again`);
      onRetried();
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  };
  if (!failures.length) return <p className="px-5 py-6 text-[13px] text-muted">No failed jobs.</p>;
  return (
    <>
      {dead.length ? (
        <div className="flex items-center justify-between border-b border-border px-5 py-2.5 text-[12.5px]">
          <span className="text-muted">{dead.length} jobs gave up after their retries.</span>
          <Button size="sm" onClick={retry} icon={<RotateCcw size={13} aria-hidden />}>
            Retry all
          </Button>
        </div>
      ) : null}
      <Table>
        <thead>
          <tr>
            <Th>Job</Th>
            <Th>State</Th>
            <Th>Error</Th>
            <Th>When</Th>
          </tr>
        </thead>
        <tbody>
          {failures.map((f) => (
            <tr key={f.id}>
              <Td>
                <div className="font-medium">{f.kind}</div>
                <div className="text-[12px] text-muted tabular">
                  #{f.id}, {f.lane}, {f.attempts} attempts
                </div>
              </Td>
              <Td>{f.status === "dead" ? <StatusBadge state="fail">Gave up</StatusBadge> : <StatusBadge state="warn">Will retry</StatusBadge>}</Td>
              <Td className="max-w-[420px]">
                <span className="line-clamp-2 text-[12.5px] text-muted" title={f.last_error}>
                  {f.last_error || "—"}
                </span>
              </Td>
              <Td className="whitespace-nowrap text-muted">{relativeTime(f.finished_at)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </>
  );
}

function Monitor() {
  const params = useSearchParams();
  const router = useRouter();
  const [minutes, setMinutes] = useState<(typeof WINDOWS)[number]["value"]>("60");
  const { data, error, reload } = useApi<Summary>(`/monitor/summary?minutes=${minutes}`, { refreshMs: 30_000 });
  const { data: series } = useApi<Timeseries>(`/monitor/timeseries?minutes=${minutes}`, { refreshMs: 30_000 });
  const { data: traces } = useApi<{ traces: TraceRow[] }>("/monitor/traces?kind=chat&limit=25", { refreshMs: 30_000 });
  const { data: checks } = useApi<{ checks: Check[] }>("/health/details");
  const [live, setLive] = useState<{ processes: Process[]; queue: Queue; alerts: number } | null>(null);
  const connected = useLive(setLive);
  const trace = params.get("trace");

  useEffect(() => {
    document.title = "Monitor";
  }, []);

  const windowMinutes = Number(minutes);
  const questions = useMemo(() => bucketize((series?.chat ?? []).map((r) => ({ minute: r.minute, value: r.requests })), windowMinutes, "sum"), [series, windowMinutes]);
  const latency = useMemo(() => bucketize((series?.chat ?? []).map((r) => ({ minute: r.minute, value: r.p95_ms })), windowMinutes, "max"), [series, windowMinutes]);
  const kinds = useMemo(() => {
    const names = [...new Set((series?.calls ?? []).map((c) => c.kind))].filter((k) => k in KIND_NAMES);
    return names.map((kind) => ({
      kind,
      points: bucketize((series?.calls ?? []).filter((c) => c.kind === kind).map((c) => ({ minute: c.minute, value: c.calls })), windowMinutes, "sum"),
    }));
  }, [series, windowMinutes]);

  const processes = live?.processes ?? data?.processes ?? [];
  const queue = live?.queue ?? data?.queue;
  const budget = data?.cost.budget_usd ?? 0;
  const spent = data?.cost.today_usd ?? 0;
  const failedChecks = checks?.checks.filter((c) => c.status !== "ok") ?? [];

  return (
    <div>
      <PageHeader
        title="Monitor"
        description="Questions, model calls, cost, queues and workers across every process, from the database rollups."
        actions={
          <>
            <span className="mr-1 inline-flex items-center gap-1.5 text-[12.5px] text-muted" title={connected ? "Receiving live updates every 2 seconds" : "Live updates paused; figures refresh every 30 seconds"}>
              <span className={clsx("inline-block h-1.5 w-1.5 rounded-full", connected ? "pulse-dot bg-success" : "bg-muted")} aria-hidden />
              {connected ? "Live" : "Paused"}
            </span>
            <Tabs value={minutes} onChange={setMinutes} items={WINDOWS.map((w) => ({ value: w.value, label: w.label }))} />
          </>
        }
      />
      <div className="space-y-6 px-6 py-6 lg:px-10">
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading the monitor" /> : null}
        {data ? (
          <>
            {data.alerts.length ? (
              <div className="space-y-2">
                {data.alerts.map((a) => (
                  <div key={a.id} className={clsx("flex flex-wrap items-center gap-3 rounded-xl border px-4 py-2.5 text-[13px]", a.severity === "critical" ? "border-danger/40 bg-danger-soft" : "border-warning/40 bg-warning-soft")}>
                    <StatusBadge state={a.severity === "critical" ? "fail" : "warn"}>{a.severity === "critical" ? "Critical" : "Warning"}</StatusBadge>
                    <span className="min-w-0 flex-1">{a.message}</span>
                    <span className="text-[12px] text-muted">
                      {a.count > 1 ? `${a.count} times, ` : ""}last {relativeTime(a.last_seen_at)}
                    </span>
                  </div>
                ))}
              </div>
            ) : null}

            <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
              <Stat label="Questions" value={compact(data.chat.requests)} detail={data.chat.errors ? `${data.chat.errors} did not finish` : "All finished"} />
              <Stat label="Answer time, typical" value={ms(data.chat.p50_ms)} detail={`Slowest 5%: ${ms(data.chat.p95_ms)}`} />
              <Stat label="Model cost today" value={usd(spent)} detail={budget ? `of ${usd(budget)} daily budget` : "No daily budget set"}>
                {budget ? <Meter value={spent} max={budget} tone={spent > budget * 0.9 ? "danger" : spent > budget * 0.7 ? "warning" : "neutral"} label="Share of daily budget used" /> : null}
              </Stat>
              <Stat
                label="Library"
                value={compact(data.content.documents)}
                detail={`${compact(data.content.pages)} pages, ${compact(data.content.chunks)} passages${data.content.in_progress ? `, ${compact(data.content.in_progress)} being read` : ""}`}
              />
            </div>

            <div className="grid gap-4 xl:grid-cols-2">
              <Section title="Questions" description="Per minute in short windows, per 5 or 15 minutes in long ones">
                <div className="px-3 py-3">
                  <LineChart points={questions} label="Questions over time" integer />
                </div>
              </Section>
              <Section title="Answer time" description="Slowest 5% of answers in each period">
                <div className="px-3 py-3">
                  <LineChart points={latency} label="Answer time over time (95th percentile)" format={(v) => ms(v)} area={false} />
                </div>
              </Section>
            </div>

            <Section title="Model calls" description="Every call to a model, by role, in this window">
              <ModelCalls rows={data.models} />
            </Section>

            {kinds.length ? (
              <Section title="Calls over time" description="One chart per kind of call, each on its own scale">
                <div className="grid gap-px bg-border sm:grid-cols-2 xl:grid-cols-3">
                  {kinds.map((k) => (
                    <div key={k.kind} className="bg-surface-raised px-3 pb-2 pt-3">
                      <div className="px-2 text-[12.5px] font-medium">{KIND_NAMES[k.kind]}</div>
                      <LineChart points={k.points} label={`${KIND_NAMES[k.kind]} per period`} height={110} integer />
                    </div>
                  ))}
                  <GridFillers count={kinds.length} layout="2-3" />
                </div>
              </Section>
            ) : null}

            <div className="grid gap-4 xl:grid-cols-2">
              <Section title="Queues" description="Ingestion lanes; permission changes never wait behind bulk work">
                {queue ? <Queues queue={queue} /> : null}
              </Section>
              <Section title="Processes" description="Heartbeats every 2 seconds">
                <Processes processes={processes} />
              </Section>
            </div>

            <Section title="Cache" description="Redis: shared between all processes, emptied by any change to the library">
              <CachePanel cache={data.cache} />
            </Section>

            <Section title="Failed jobs" description={data.content.failed_items ? `${compact(data.content.failed_items)} files could not be read` : undefined}>
              <Failures failures={data.failures} onRetried={() => void reload()} />
            </Section>

            {data.model_errors.length ? (
              <Section title="Model errors" description="Failed model calls in this window">
                <Table>
                  <thead>
                    <tr>
                      <Th>Model</Th>
                      <Th>Error</Th>
                      <Th>When</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {data.model_errors.map((e, i) => (
                      <tr key={i} className={e.trace_id ? "cursor-pointer hover:bg-surface-hover" : undefined} onClick={() => e.trace_id && router.replace(`/monitor?trace=${e.trace_id}`, { scroll: false })}>
                        <Td className="tabular">
                          {e.provider}/{e.model}
                          <div className="text-[12px] text-muted">{e.role}</div>
                        </Td>
                        <Td className="max-w-[460px]">
                          <span className="line-clamp-2 text-[12.5px] text-danger">{e.error}</span>
                        </Td>
                        <Td className="whitespace-nowrap text-muted">{relativeTime(e.started_at)}</Td>
                      </tr>
                    ))}
                  </tbody>
                </Table>
              </Section>
            ) : null}

            <Section title="Recent questions" description="Select one to see each stage and model call">
              {traces?.traces.length ? (
                <Table>
                  <thead>
                    <tr>
                      <Th>When</Th>
                      <Th>Who</Th>
                      <Th>Outcome</Th>
                      <Th>Model</Th>
                      <Th align="right">Time</Th>
                    </tr>
                  </thead>
                  <tbody>
                    {traces.traces.map((t) => {
                      const status = String(t.attrs?.status ?? t.status);
                      return (
                        <tr key={t.id} tabIndex={0} className="cursor-pointer hover:bg-surface-hover" onClick={() => router.replace(`/monitor?trace=${t.id}`, { scroll: false })} onKeyDown={(e) => e.key === "Enter" && router.replace(`/monitor?trace=${t.id}`, { scroll: false })}>
                          <Td className="whitespace-nowrap text-muted">{relativeTime(t.started_at)}</Td>
                          <Td className="max-w-[220px] truncate">{t.email ?? "—"}</Td>
                          <Td>
                            {status === "ok" ? (
                              <StatusBadge state="ok">Answered</StatusBadge>
                            ) : status === "not_found" ? (
                              <StatusBadge state="info">Not in documents</StatusBadge>
                            ) : status === "unverified" ? (
                              <StatusBadge state="warn">Unverified</StatusBadge>
                            ) : (
                              <StatusBadge state="fail">{status}</StatusBadge>
                            )}
                          </Td>
                          <Td className="max-w-[260px] truncate text-muted tabular" title={String(t.attrs?.model ?? "")}>
                            {t.attrs?.model ? modelLabel(splitSpec(String(t.attrs.model)).provider, splitSpec(String(t.attrs.model)).model) : "—"}
                          </Td>
                          <Td align="right">{ms(t.duration_ms)}</Td>
                        </tr>
                      );
                    })}
                  </tbody>
                </Table>
              ) : (
                <p className="px-5 py-6 text-[13px] text-muted">No questions in the last 7 days.</p>
              )}
            </Section>

            <Section
              title="System checks"
              description="Configuration and connectivity, checked when this page opened"
              actions={checks ? failedChecks.length ? <StatusBadge state="warn">{failedChecks.length} to look at</StatusBadge> : <StatusBadge state="ok">All passing</StatusBadge> : null}
            >
              {checks ? (
                <div className="grid gap-px bg-border sm:grid-cols-2">
                  {checks.checks.map((c) => {
                    const icon = /database/i.test(c.name) ? "postgresql" : /qdrant/i.test(c.name) ? "qdrant" : /object store/i.test(c.name) ? "storage" : /model server/i.test(c.name) ? "onnx" : null;
                    const spec = c.name.startsWith("role ") && /^[a-z_]+\/\S+$/.test(c.detail) ? splitSpec(c.detail) : null;
                    return (
                      <div key={c.name} className="flex items-start gap-3 bg-surface-raised px-5 py-3">
                        {spec ? <ModelIcon provider={spec.provider} model={spec.model} size={26} /> : icon ? <BrandIcon name={icon} size={26} /> : <span className="w-[26px] shrink-0" />}
                        <div className="min-w-0 flex-1">
                          <div className="flex items-center gap-2 text-[13px]">
                            <span className="truncate font-medium">{c.name.startsWith("role ") ? ROLE_NAMES[c.name.slice(5)] ?? c.name : titleCaseCheck(c.name)}</span>
                            {c.name.startsWith("role ") ? <code className="text-[11px] text-muted">{c.name.slice(5)}</code> : null}
                            {c.status === "ok" ? <StatusBadge state="ok">OK</StatusBadge> : c.status === "warn" ? <StatusBadge state="warn">Check</StatusBadge> : <StatusBadge state="fail">Failing</StatusBadge>}
                          </div>
                          <div className="mt-0.5 line-clamp-2 text-[12px] text-muted">{c.detail}</div>
                        </div>
                      </div>
                    );
                  })}
                  <GridFillers count={checks.checks.length} layout="2" />
                </div>
              ) : (
                <div className="px-5 py-4">
                  <Spinner label="Running checks" />
                </div>
              )}
            </Section>
            <p className="text-[12px] text-muted">
              Prometheus can scrape the same figures from <code>/metrics</code> when <code>PROMETHEUS_METRICS=true</code>; Grafana is optional. <Badge tone="outline">Retention follows MONITOR_RETENTION_DAYS</Badge>
            </p>
          </>
        ) : null}
      </div>
      {trace ? <TraceDrawer traceId={trace} onClose={() => router.replace("/monitor", { scroll: false })} /> : null}
    </div>
  );
}

export default function MonitorPage() {
  return (
    <Suspense fallback={null}>
      <Monitor />
    </Suspense>
  );
}
