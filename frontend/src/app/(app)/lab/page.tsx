"use client";

import clsx from "clsx";
import { Play } from "lucide-react";
import Link from "next/link";
import { useEffect, useState, type FormEvent } from "react";

import { Button } from "@/components/ui/button";
import { Badge, ErrorNote, Spinner } from "@/components/ui/feedback";
import { Switch, Textarea } from "@/components/ui/fields";
import { GridFillers, PageHeader, Section, Table, Td, Th } from "@/components/ui/layout";
import { api, errorMessage } from "@/lib/api";
import { compact, docTypeLabel, ms } from "@/lib/format";

type Hit = { id: string; score: number; doc: string; page: number };
type Ranked = { id: string; doc: string; page: number; legs: Record<string, number>; fused: number; rerank: number | null; final: number; demoted: boolean; text: string };
type Passage = { n: number; chunk_id: string; document_id: string; doc_number: string; title: string; page_start: number; page_end: number; section_path: string; kind: string; text: string; score: number; file_name: string; project: string; doc_type: string };
type Debug = {
  plan: { query: string; language: string; query_type: string; identifiers: string[]; english: string | null; hard: boolean; filters: Record<string, unknown>; boosts: Record<string, string[]>; reasons: string[]; router: string };
  weights: Record<string, number> | null;
  passages: Passage[];
  documents: { document_id: string; doc_number: string; title: string; score: number; pages: number[]; doc_type: string }[];
  timings_ms: Record<string, number>;
  debug: { collection: string; visible_chunks: number; exact_scan: boolean; legs: Record<string, Hit[]>; ranked: Ranked[] };
  models: { embedder: string; reranker: string };
};

const LEG_NAMES: Record<string, string> = { dense: "Meaning (dense)", sparse: "Words (BM25)", sparse_en: "Words, English", exact: "Exact identifier" };
const TYPE_NAMES: Record<string, string> = { identifier: "Identifier lookup", keyword: "Keyword search", question: "Question", non_english: "Non-English question" };
const STAGES = ["embed", "search", "fuse", "rerank", "candidates"];
const STAGE_NAMES: Record<string, string> = { embed: "Embed the question", search: "Search all legs", fuse: "Fuse and group families", rerank: "Rerank", candidates: "Check access and expand passages" };

function Timings({ timings }: { timings: Record<string, number> }) {
  const total = timings.total || STAGES.reduce((a, s) => a + (timings[s] || 0), 0) || 1;
  return (
    <div className="space-y-2 px-5 py-4">
      {STAGES.filter((s) => s in timings).map((stage) => (
        <div key={stage} className="grid grid-cols-[minmax(0,200px)_1fr_64px] items-center gap-3 text-[12.5px]">
          <span className="truncate text-muted">{STAGE_NAMES[stage]}</span>
          <span className="h-2 overflow-hidden rounded-full bg-surface">
            <span className="block h-full rounded-full bg-foreground/75" style={{ width: `${Math.max(1, (timings[stage] / total) * 100)}%` }} />
          </span>
          <span className="text-right tabular">{ms(timings[stage])}</span>
        </div>
      ))}
      <div className="grid grid-cols-[minmax(0,200px)_1fr_64px] gap-3 border-t border-border pt-2 text-[12.5px] font-medium">
        <span>Total</span>
        <span />
        <span className="text-right tabular">{ms(total)}</span>
      </div>
    </div>
  );
}

function PassageCard({ p }: { p: Passage }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-border px-5 py-4 last:border-b-0">
      <div className="flex flex-wrap items-center gap-2 text-[12.5px]">
        <span className="flex h-5 min-w-5 items-center justify-center rounded-[4px] border border-border-strong px-1 text-[11px] font-semibold tabular">{p.n}</span>
        <Link href={`/documents/${p.document_id}?page=${p.page_start}`} className="font-medium tabular underline-offset-2 hover:underline">
          {p.doc_number || p.file_name}
        </Link>
        <span className="text-muted">
          p. {p.page_start}
          {p.page_end !== p.page_start ? `–${p.page_end}` : ""}
        </span>
        <Badge tone="outline">{p.kind}</Badge>
        <span className="ml-auto text-muted tabular">score {p.score.toFixed(3)}</span>
      </div>
      {p.section_path ? <div className="mt-1 text-[12px] text-muted">{p.section_path}</div> : null}
      <p dir="auto" className={clsx("source-text mt-2 whitespace-pre-wrap text-foreground/90", !open && "line-clamp-4")}>
        {p.text}
      </p>
      <button type="button" onClick={() => setOpen(!open)} className="mt-1 text-[12px] text-muted hover:text-foreground">
        {open ? "Show less" : "Show the whole passage"}
      </button>
    </div>
  );
}

export default function LabPage() {
  const [question, setQuestion] = useState("");
  const [rerank, setRerank] = useState(true);
  const [planner, setPlanner] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<Debug | null>(null);

  useEffect(() => {
    document.title = "Retrieval lab";
  }, []);

  const run = async (event?: FormEvent) => {
    event?.preventDefault();
    if (!question.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setResult(await api<Debug>("/retrieval/debug", { body: { question: question.trim(), rerank, use_router_model: planner } }));
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const legs = result ? Object.keys(result.debug.legs) : [];

  return (
    <div>
      <PageHeader title="Retrieval lab" description="Every stage of one search: how the question was read, what each search leg found, how the results were fused and reranked, and the passages an answer would get. Runs with your own access." />
      <div className="space-y-6 px-6 py-6 lg:px-10">
        <form onSubmit={run} className="rounded-2xl border border-border bg-surface-raised p-4">
          <label htmlFor="lab-question" className="sr-only">
            Question
          </label>
          <Textarea
            id="lab-question"
            rows={2}
            dir="auto"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                void run();
              }
            }}
            placeholder="What concrete grade is specified for the columns?"
            className="border-0 bg-transparent px-1 text-[15px] focus-visible:outline-none"
          />
          <div className="mt-2 flex flex-wrap items-center gap-5 border-t border-border pt-3 text-[13px]">
            <label className="flex items-center gap-2">
              <Switch checked={rerank} onChange={setRerank} label="Rerank" />
              Rerank
            </label>
            <label className="flex items-center gap-2">
              <Switch checked={planner} onChange={setPlanner} label="Use the planning model" />
              Use the planning model
            </label>
            <Button type="submit" variant="primary" className="ml-auto" disabled={busy || !question.trim()} icon={<Play size={14} aria-hidden />}>
              {busy ? "Searching" : "Search"}
            </Button>
          </div>
        </form>

        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {busy && !result ? <Spinner label="Searching" /> : null}

        {result ? (
          <>
            <div className="grid gap-4 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <Section title="How the question was read" description={`${result.plan.router === "llm" ? "Planning model" : "Rules"}, ${result.plan.language.toUpperCase()}`}>
                <dl className="grid grid-cols-[130px_1fr] gap-x-4 gap-y-2 px-5 py-4 text-[13px]">
                  <dt className="text-muted">Kind</dt>
                  <dd>
                    {TYPE_NAMES[result.plan.query_type] ?? result.plan.query_type}
                    {result.plan.hard ? <Badge className="ml-2" tone="outline">Multi-part</Badge> : null}
                  </dd>
                  <dt className="text-muted">Search text</dt>
                  <dd dir="auto">{result.plan.query}</dd>
                  {result.plan.english ? (
                    <>
                      <dt className="text-muted">In English</dt>
                      <dd>{result.plan.english}</dd>
                    </>
                  ) : null}
                  <dt className="text-muted">Identifiers</dt>
                  <dd className="flex flex-wrap gap-1.5">
                    {result.plan.identifiers.length ? result.plan.identifiers.map((id) => <Badge key={id} tone="outline">{id}</Badge>) : <span className="text-muted">None</span>}
                  </dd>
                  <dt className="text-muted">Filters</dt>
                  <dd className="flex flex-wrap gap-1.5">
                    {Object.entries(result.plan.filters).map(([k, v]) => (
                      <Badge key={k} tone="outline">
                        {k.replace(/_/g, " ")}: {Array.isArray(v) ? v.join(", ") : String(v)}
                      </Badge>
                    ))}
                  </dd>
                  {Object.keys(result.plan.boosts || {}).length ? (
                    <>
                      <dt className="text-muted">Preferences</dt>
                      <dd className="flex flex-wrap gap-1.5">
                        {Object.entries(result.plan.boosts).flatMap(([k, values]) => values.map((v) => <Badge key={`${k}-${v}`}>{k === "doc_types" ? docTypeLabel(v) : v}</Badge>))}
                      </dd>
                    </>
                  ) : null}
                  {result.plan.reasons.length ? (
                    <>
                      <dt className="text-muted">Why</dt>
                      <dd>
                        <ul className="list-disc space-y-0.5 pl-4 text-[12.5px] text-muted">
                          {result.plan.reasons.map((r, i) => (
                            <li key={i}>{r}</li>
                          ))}
                        </ul>
                      </dd>
                    </>
                  ) : null}
                  <dt className="text-muted">Leg weights</dt>
                  <dd className="flex flex-wrap gap-1.5">
                    {Object.entries(result.weights ?? {}).map(([leg, w]) => (
                      <Badge key={leg} tone="outline">
                        {LEG_NAMES[leg] ?? leg} {w}
                      </Badge>
                    ))}
                  </dd>
                </dl>
              </Section>
              <Section title="Time" description={`${compact(result.debug.visible_chunks)} passages visible to you${result.debug.exact_scan ? ", scanned exactly" : ", HNSW index"}`}>
                <Timings timings={result.timings_ms} />
                <div className="border-t border-border px-5 py-3 text-[12px] text-muted">
                  <div className="truncate tabular">Embedder {result.models.embedder}</div>
                  <div className="truncate tabular">Reranker {result.models.reranker}</div>
                  <div className="truncate tabular">Index {result.debug.collection}</div>
                </div>
              </Section>
            </div>

            <Section title="What each leg found" description="Top hits per leg before fusion">
              <div className="grid gap-px bg-border md:grid-cols-2 xl:grid-cols-4">
                {legs.map((leg) => (
                  <div key={leg} className="min-w-0 bg-surface-raised px-4 py-3">
                    <div className="mb-2 text-[12.5px] font-medium">{LEG_NAMES[leg] ?? leg}</div>
                    {result.debug.legs[leg].length ? (
                      <ol className="space-y-1 text-[12px]">
                        {result.debug.legs[leg].slice(0, 10).map((h) => (
                          <li key={h.id} className="flex gap-2">
                            <span className="min-w-0 flex-1 truncate tabular">
                              {h.doc} <span className="text-muted">p. {h.page}</span>
                            </span>
                            <span className="text-muted tabular">{h.score.toFixed(3)}</span>
                          </li>
                        ))}
                      </ol>
                    ) : (
                      <p className="text-[12px] text-muted">No hits</p>
                    )}
                  </div>
                ))}
                <GridFillers count={legs.length} layout="2-4" />
              </div>
            </Section>

            <Section title="Fused and reranked" description="Each leg's score is scaled to its best hit, weighted, and summed; reranking reorders the top candidates">
              <Table>
                <thead>
                  <tr>
                    <Th>#</Th>
                    <Th>Passage</Th>
                    {legs.map((leg) => (
                      <Th key={leg} align="right">
                        {leg}
                      </Th>
                    ))}
                    <Th align="right">Fused</Th>
                    <Th align="right">Rerank</Th>
                    <Th align="right">Final</Th>
                  </tr>
                </thead>
                <tbody>
                  {result.debug.ranked.slice(0, 25).map((r, i) => (
                    <tr key={r.id} className={r.demoted ? "text-muted" : undefined}>
                      <Td className="tabular text-muted">{i + 1}</Td>
                      <Td className="max-w-[360px]">
                        <div className="truncate tabular" title={r.text}>
                          {r.doc} <span className="text-muted">p. {r.page}</span>
                          {r.demoted ? <Badge className="ml-2" tone="outline" title="A copy of a document already ranked higher">Duplicate</Badge> : null}
                        </div>
                      </Td>
                      {legs.map((leg) => (
                        <Td key={leg} align="right" className="text-muted">
                          {r.legs[leg] !== undefined ? r.legs[leg].toFixed(3) : "—"}
                        </Td>
                      ))}
                      <Td align="right">{r.fused.toFixed(3)}</Td>
                      <Td align="right">{r.rerank === null ? "—" : r.rerank.toFixed(3)}</Td>
                      <Td align="right" className="font-medium">
                        {r.final.toFixed(3)}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Section>

            <Section title={`Passages an answer would get (${result.passages.length})`} description="After access checks and expansion to the surrounding text">
              {result.passages.length ? result.passages.map((p) => <PassageCard key={p.chunk_id} p={p} />) : <p className="px-5 py-6 text-[13px] text-muted">Nothing found. An answer would say the documents do not cover this.</p>}
            </Section>
          </>
        ) : null}
      </div>
    </div>
  );
}
