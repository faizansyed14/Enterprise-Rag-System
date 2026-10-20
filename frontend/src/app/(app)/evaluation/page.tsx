"use client";

import clsx from "clsx";
import { CircleCheck, CircleX, Play } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { EmptyState, ErrorNote, Spinner, StatusBadge } from "@/components/ui/feedback";
import { Field, Input, Select, Switch } from "@/components/ui/fields";
import { PageHeader, Section, Stat, Table, Td, Th } from "@/components/ui/layout";
import { Drawer } from "@/components/ui/overlay";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { ms, pct, relativeTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";

type Metrics = Record<string, number>;
type Run = { id: string; name: string; golden_set: string; status: string; config: Record<string, unknown>; metrics: Metrics | null; error: string | null; created_at: string; finished_at: string | null };
type Item = { item_id: string; question: string; expected: Record<string, unknown>; retrieved: { doc_number?: string; title?: string; page_start?: number }[] | null; answer: string | null; metrics: Record<string, number | boolean | null>; latency_ms: number | null };

const HEADLINE: { key: string; label: string; detail: string }[] = [
  { key: "hit@5", label: "Right document in top 5", detail: "Share of questions whose expected document is among the first five" },
  { key: "mrr", label: "Rank of the right document", detail: "Mean reciprocal rank; 1.0 means always first" },
  { key: "facts", label: "Facts in the answer", detail: "Share of expected facts the answer states" },
  { key: "citations_verified", label: "Quotes found on the page", detail: "Share of citations whose quote was found on the cited page" },
];

function RunState({ run }: { run: Run }) {
  if (run.status === "done") return <StatusBadge state="ok">Done</StatusBadge>;
  if (run.status === "failed") return <StatusBadge state="fail">Failed</StatusBadge>;
  if (run.status === "running") return <StatusBadge state="info">Running</StatusBadge>;
  return <StatusBadge state="info">Queued</StatusBadge>;
}

function Value({ value, kind = "pct" }: { value: number | undefined; kind?: "pct" | "ms" | "num" }) {
  if (value === undefined || value === null) return <span className="text-muted">—</span>;
  return <>{kind === "pct" ? pct(value) : kind === "ms" ? ms(value) : value.toFixed(3)}</>;
}

function RunDrawer({ runId, onClose }: { runId: string; onClose: () => void }) {
  const { data, error } = useApi<Run & { items: Item[] }>(`/eval/runs/${runId}`);
  return (
    <Drawer open onClose={onClose} width="min(820px, 100vw)" title={data?.name ?? "Evaluation run"} subtitle={data ? `${data.golden_set} set, ${data.items.length} questions` : undefined}>
      <div className="px-5 py-4">
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading the run" /> : null}
        {data ? (
          <div className="space-y-3">
            {data.items.map((item) => {
              const hit = item.metrics["hit@5"];
              const notFound = item.expected && (item.expected as { not_found?: boolean }).not_found;
              return (
                <div key={item.item_id} className="rounded-xl border border-border px-4 py-3">
                  <div className="flex items-start gap-2">
                    {hit === true || (notFound && item.metrics.not_found_correct) ? (
                      <CircleCheck size={15} className="mt-0.5 shrink-0 text-success" aria-label="Passed" />
                    ) : (
                      <CircleX size={15} className="mt-0.5 shrink-0 text-danger" aria-label="Missed" />
                    )}
                    <div className="min-w-0 flex-1">
                      <div dir="auto" className="text-[13.5px] font-medium">
                        {item.question}
                      </div>
                      <div className="mt-1 flex flex-wrap gap-x-4 gap-y-1 text-[12px] text-muted">
                        {typeof item.metrics.rank === "number" ? <span>Rank {item.metrics.rank}</span> : notFound ? <span>Should not be found</span> : <span>Not retrieved</span>}
                        {typeof item.metrics.facts === "number" ? <span>Facts {pct(item.metrics.facts)}</span> : null}
                        {item.metrics.no_wrong_facts === false ? <span className="text-danger">States a wrong fact</span> : null}
                        {typeof item.metrics.citations_verified === "number" ? <span>Quotes found {pct(item.metrics.citations_verified)}</span> : null}
                        {item.latency_ms ? <span className="tabular">{ms(item.latency_ms)}</span> : null}
                      </div>
                      {item.answer ? (
                        <p dir="auto" className="mt-2 line-clamp-3 text-[13px] text-foreground/85">
                          {item.answer}
                        </p>
                      ) : null}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        ) : null}
      </div>
    </Drawer>
  );
}

export default function EvaluationPage() {
  const toast = useToast();
  const { data, error, reload } = useApi<{ runs: Run[] }>("/eval/runs", { refreshMs: 5000 });
  const { data: sets } = useApi<{ sets: string[] }>("/eval/sets");
  const [goldenSet, setGoldenSet] = useState("demo");
  const [name, setName] = useState("");
  const [answer, setAnswer] = useState(true);
  const [busy, setBusy] = useState(false);
  const [open, setOpen] = useState<string | null>(null);

  useEffect(() => {
    document.title = "Evaluation";
  }, []);

  const start = async () => {
    setBusy(true);
    try {
      await api("/eval/runs", { body: { name: name.trim(), golden_set: goldenSet, answer } });
      toast("Evaluation queued");
      setName("");
      await reload();
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };

  const runs = data?.runs ?? [];
  const latest = runs.find((r) => r.status === "done");
  const previous = latest ? runs.filter((r) => r.status === "done" && r.golden_set === latest.golden_set)[1] : undefined;

  return (
    <div>
      <PageHeader title="Evaluation" description="Golden questions with known answers, run against the live index and models. Run one after changing a model, the chunker or the fusion weights, and compare." />
      <div className="space-y-6 px-6 py-6 lg:px-10">
        <Section title="New run">
          <div className="flex flex-wrap items-end gap-4 px-5 py-4">
            <div className="w-[200px]">
              <Field label="Question set">
                <Select value={goldenSet} onChange={(e) => setGoldenSet(e.target.value)}>
                  {(sets?.sets ?? ["demo"]).map((s) => (
                    <option key={s} value={s}>
                      {s}
                    </option>
                  ))}
                </Select>
              </Field>
            </div>
            <div className="min-w-[220px] flex-1">
              <Field label="Name (optional)">
                <Input value={name} onChange={(e) => setName(e.target.value)} placeholder="After switching to bge-m3" />
              </Field>
            </div>
            <label className="flex h-9 items-center gap-2 text-[13px]">
              <Switch checked={answer} onChange={setAnswer} label="Also write answers" />
              Also write answers
            </label>
            <Button variant="primary" onClick={start} disabled={busy} icon={<Play size={14} aria-hidden />}>
              Run
            </Button>
          </div>
        </Section>

        {latest?.metrics ? (
          <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
            {HEADLINE.map((h) => {
              const value = latest.metrics?.[h.key];
              const before = previous?.metrics?.[h.key];
              const delta = value !== undefined && before !== undefined ? value - before : null;
              return (
                <Stat
                  key={h.key}
                  label={h.label}
                  value={h.key === "mrr" ? (value ?? 0).toFixed(2) : pct(value)}
                  detail={
                    <span title={h.detail}>
                      {delta === null || Math.abs(delta) < 0.0005 ? (
                        `${latest.name}`
                      ) : (
                        <span className={clsx(delta > 0 ? "text-success" : "text-danger")}>
                          {delta > 0 ? "Up" : "Down"} {h.key === "mrr" ? Math.abs(delta).toFixed(2) : pct(Math.abs(delta))} from the previous run
                        </span>
                      )}
                    </span>
                  }
                />
              );
            })}
          </div>
        ) : null}

        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading runs" /> : null}
        {data && !runs.length ? <EmptyState title="No runs yet">Run the demo set to see how retrieval and answers score on the sample library.</EmptyState> : null}
        {runs.length ? (
          <Section title="Runs">
            <Table>
              <thead>
                <tr>
                  <Th>Run</Th>
                  <Th>State</Th>
                  <Th align="right">Top 1</Th>
                  <Th align="right">Top 5</Th>
                  <Th align="right">MRR</Th>
                  <Th align="right">Right page</Th>
                  <Th align="right">Facts</Th>
                  <Th align="right">Not-found right</Th>
                  <Th align="right">Typical time</Th>
                  <Th>When</Th>
                </tr>
              </thead>
              <tbody>
                {runs.map((r) => (
                  <tr key={r.id} tabIndex={0} onClick={() => setOpen(r.id)} onKeyDown={(e) => e.key === "Enter" && setOpen(r.id)} className="cursor-pointer hover:bg-surface-hover">
                    <Td className="max-w-[260px]">
                      <div className="truncate font-medium">{r.name}</div>
                      <div className="truncate text-[12px] text-muted tabular">{String(r.config?.embedder ?? "")}</div>
                      {r.error ? <div className="truncate text-[12px] text-danger">{r.error}</div> : null}
                    </Td>
                    <Td>
                      <RunState run={r} />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.["hit@1"]} />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.["hit@5"]} />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.mrr} kind="num" />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.["page_hit@5"]} />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.facts} />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.not_found_correct} />
                    </Td>
                    <Td align="right">
                      <Value value={r.metrics?.latency_p50_ms} kind="ms" />
                    </Td>
                    <Td className="whitespace-nowrap text-muted">{relativeTime(r.created_at)}</Td>
                  </tr>
                ))}
              </tbody>
            </Table>
          </Section>
        ) : null}
      </div>
      {open ? <RunDrawer runId={open} onClose={() => setOpen(null)} /> : null}
    </div>
  );
}
