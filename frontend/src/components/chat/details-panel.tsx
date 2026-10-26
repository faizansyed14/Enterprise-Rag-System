"use client";

import clsx from "clsx";
import {
  AlertTriangle,
  BookOpenText,
  BrainCircuit,
  Calculator,
  Check,
  ChevronRight,
  ClipboardCheck,
  Copy,
  Database,
  Download,
  FileCheck2,
  FileSearch,
  Files,
  Fingerprint,
  ListChecks,
  Loader2,
  MessageSquareText,
  Route,
  Search,
  Sparkles,
  Split,
  Wrench,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { useToast } from "@/components/ui/toast";
import { compact, ms, usd } from "@/lib/format";
import { useEscape } from "@/lib/hooks";
import type { ChatMessage, TraceItem } from "@/lib/types";

/* ---- what each kind of step is ------------------------------------------------------------------------------------- */

const KINDS: Record<string, { icon: typeof Search; group: Group; label: string }> = {
  question: { icon: MessageSquareText, group: "search", label: "Question" },
  cache: { icon: Database, group: "search", label: "Cache" },
  plan: { icon: Route, group: "search", label: "Plan" },
  embed: { icon: Fingerprint, group: "search", label: "Embedding" },
  search: { icon: Search, group: "search", label: "Search" },
  named: { icon: FileSearch, group: "search", label: "Named files" },
  subquestion: { icon: Split, group: "search", label: "Sub-question" },
  strategy: { icon: BrainCircuit, group: "reading", label: "Strategy" },
  read: { icon: BookOpenText, group: "reading", label: "Read" },
  candidates: { icon: Files, group: "reading", label: "Candidates" },
  verdicts: { icon: ListChecks, group: "reading", label: "Relevance" },
  tool: { icon: Wrench, group: "reading", label: "Tool" },
  evidence: { icon: ClipboardCheck, group: "reading", label: "Evidence" },
  llm: { icon: Sparkles, group: "model", label: "Model call" },
  check: { icon: FileCheck2, group: "checks", label: "Check" },
  calc: { icon: Calculator, group: "checks", label: "Arithmetic" },
  citations: { icon: FileCheck2, group: "checks", label: "Citations" },
  done: { icon: Check, group: "checks", label: "Finished" },
};

type Group = "all" | "model" | "search" | "reading" | "checks";
const GROUPS: { id: Group; label: string }[] = [
  { id: "all", label: "All" },
  { id: "model", label: "Model calls" },
  { id: "search", label: "Search" },
  { id: "reading", label: "Reading" },
  { id: "checks", label: "Checks" },
];

/* ---- small building blocks ----------------------------------------------------------------------------------------- */

const num = (value: unknown, digits = 4) =>
  typeof value === "number" ? (Number.isInteger(value) ? value.toLocaleString() : value.toFixed(digits)) : value == null ? "—" : String(value);

function show(value: unknown): ReactNode {
  if (value === null || value === undefined || value === "") return <span className="text-muted">—</span>;
  if (typeof value === "boolean") return value ? "yes" : "no";
  if (typeof value === "number") return <span className="tabular">{num(value)}</span>;
  if (Array.isArray(value)) {
    if (!value.length) return <span className="text-muted">none</span>;
    if (value.every((v) => typeof v !== "object")) return value.join(", ");
  }
  if (typeof value === "object") return <code className="whitespace-pre-wrap break-words text-[11.5px]">{JSON.stringify(value, null, 1)}</code>;
  return <span className="whitespace-pre-wrap break-words">{String(value)}</span>;
}

function Fields({ rows }: { rows: [string, unknown][] }) {
  const shown = rows.filter(([, v]) => v !== undefined);
  if (!shown.length) return null;
  return (
    <dl className="grid grid-cols-[minmax(110px,max-content)_1fr] gap-x-3 gap-y-1 text-[12px]">
      {shown.map(([k, v]) => (
        <div key={k} className="contents">
          <dt className="text-muted">{k}</dt>
          <dd className="min-w-0">{show(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

function TextBlock({ text, label, open = false }: { text: string; label: ReactNode; open?: boolean }) {
  const toast = useToast();
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      toast("Copied");
    } catch {
      toast("Copying needs clipboard permission in this browser", "error");
    }
  };
  return (
    <details open={open} className="group rounded-lg border border-border">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 text-[12px] [&::-webkit-details-marker]:hidden">
        <ChevronRight size={12} className="shrink-0 text-muted transition-transform group-open:rotate-90" aria-hidden />
        <span className="min-w-0 flex-1 truncate">{label}</span>
        <span className="shrink-0 tabular text-[11px] text-muted">{text.length.toLocaleString()} chars</span>
      </summary>
      <div className="relative border-t border-border">
        <button
          type="button"
          onClick={copy}
          className="absolute right-1.5 top-1.5 rounded-md bg-background/90 p-1 text-muted hover:text-foreground"
          aria-label="Copy text"
          title="Copy"
        >
          <Copy size={12} />
        </button>
        <pre className="max-h-[420px] overflow-auto whitespace-pre-wrap break-words px-2.5 py-2 font-mono text-[11.5px] leading-[1.5]">{text}</pre>
      </div>
    </details>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="text-[11px] font-medium uppercase tracking-wide text-muted">{title}</div>
      {children}
    </div>
  );
}

function Table({ head, rows }: { head: string[]; rows: ReactNode[][] }) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border">
      <table className="w-full text-left text-[11.5px]">
        <thead className="bg-surface text-muted">
          <tr>
            {head.map((h) => (
              <th key={h} className="whitespace-nowrap px-2 py-1 font-medium">
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((cells, i) => (
            <tr key={i} className="border-t border-border/70 align-top">
              {cells.map((cell, j) => (
                <td key={j} className="px-2 py-1">
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/* ---- one renderer per kind ----------------------------------------------------------------------------------------- */

function SearchDetails({ d }: { d: Record<string, any> }) {
  const [openRow, setOpenRow] = useState<number | null>(null);
  const rerank = d.rerank ?? {};
  return (
    <>
      <Fields
        rows={[
          ["query", d.query],
          ["English", d.english ?? undefined],
          ["identifiers", d.identifiers],
          ["filters", Object.keys(d.filters ?? {}).length ? d.filters : "none"],
          ["hits per leg", Object.entries(d.legs ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")],
          ["limits", Object.entries(d.limits ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")],
          [
            "reranker",
            rerank.skipped
              ? rerank.skipped
              : rerank.model
                ? `${rerank.model}: ${rerank.pairs} pairs (${rerank.scored_now} scored now, ${rerank.from_shared_cache} from Redis, ${rerank.from_memory} from memory), blend ${rerank.blend ?? "—"}`
                : "not used",
          ],
          ["signals", d.signals],
          ["off topic", d.off_topic],
          ["timings", Object.entries(d.timings_ms ?? {}).map(([k, v]) => `${k} ${ms(v as number)}`).join(" · ")],
        ]}
      />
      <Section title={`Top ${d.candidates?.length ?? 0} candidate chunks (click a row for its text)`}>
        <Table
          head={["#", "file · page", "dense", "bm25", "exact", "fused", "rerank", "final"]}
          rows={(d.candidates ?? []).flatMap((c: any) => {
            const row: ReactNode[] = [
              c.rank,
              <button key="f" type="button" onClick={() => setOpenRow(openRow === c.rank ? null : c.rank)} className="text-left hover:underline">
                {c.file} · p.{c.page}
                {c.named ? <span className="ml-1 text-accent">named</span> : null}
                {c.demoted ? <span className="ml-1 text-warning">near-copy</span> : null}
              </button>,
              num(c.legs?.dense),
              num(c.legs?.sparse ?? c.legs?.sparse_en),
              num(c.legs?.exact),
              num(c.fused),
              num(c.rerank),
              <b key="s">{num(c.final)}</b>,
            ];
            const out = [row];
            if (openRow === c.rank) {
              out.push([
                "",
                <pre key="t" className="max-h-60 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px]">
                  {c.text}
                </pre>,
                "",
                "",
                "",
                "",
                "",
                "",
              ]);
            }
            return out;
          })}
        />
      </Section>
      <Section title={`${d.passages?.length ?? 0} passages this search returned`}>
        <div className="space-y-1">
          {(d.passages ?? []).map((p: any) => (
            <TextBlock key={p.n} text={p.text} label={`[${p.n}] ${p.file} · pp.${p.pages?.join("-")} · score ${num(p.score)}`} />
          ))}
        </div>
      </Section>
    </>
  );
}

function ModelCallDetails({ d, item }: { d: Record<string, any>; item: TraceItem }) {
  return (
    <>
      <Fields
        rows={[
          ["why", d.purpose],
          ["model", `${d.provider}/${d.model}${d.local ? " (on this server)" : ""}`],
          ["role", d.role],
          ["settings", Object.entries(d.params ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ") || "defaults"],
          ["tools offered", d.tools?.length ? d.tools : undefined],
          [
            "masked values",
            d.masked_values == null ? "no privacy shield on this call" : `${d.masked_values} replaced by placeholders before sending`,
          ],
          ["sent", `${(d.sent_chars ?? 0).toLocaleString()} characters`],
          ["tokens", d.input_tokens != null ? `${compact(d.input_tokens)} in · ${compact(d.output_tokens)} out` : item.status === "running" ? "waiting for the reply" : undefined],
          ["cost", d.cost_usd != null ? usd(d.cost_usd) : undefined],
          ["finish", d.finish_reason ?? undefined],
          ["error", d.error ?? undefined],
        ]}
      />
      <Section title="Sent to the model (exactly as it left the server)">
        <div className="space-y-1">
          {(d.messages ?? []).map((m: any, i: number) => (
            <TextBlock key={i} text={m.content + (m.tool_calls ? `\n\n[tool calls] ${JSON.stringify(m.tool_calls, null, 1)}` : "")} label={<b className="font-medium">{m.role}{m.name ? ` · ${m.name}` : ""}</b>} />
          ))}
        </div>
      </Section>
      <Section title="Reply">
        {item.status === "running" ? (
          <div className="flex items-center gap-2 text-[12px] text-muted">
            <Loader2 size={13} className="animate-spin" aria-hidden /> waiting for the model
          </div>
        ) : (
          <div className="space-y-1">
            <TextBlock text={d.reply || "(empty)"} label="reply" open />
            {d.tool_calls?.length ? <TextBlock text={JSON.stringify(d.tool_calls, null, 2)} label="tool calls" open /> : null}
          </div>
        )}
      </Section>
    </>
  );
}

function Details({ item }: { item: TraceItem }) {
  const d = item.data ?? {};
  switch (item.kind) {
    case "question":
      return (
        <Fields
          rows={[
            ["question", d.question],
            ["after the safety check", d.checked_text !== d.question ? d.checked_text : "unchanged"],
            ["blocked", d.blocked ? d.reason : false],
            ["model chosen", d.model_choice ?? "default"],
            ["filters", Object.keys(d.filters ?? {}).length ? d.filters : "none"],
            ["earlier messages", d.history?.length ? d.history.map((h: any) => `${h.role}: ${h.content}`).join("\n\n") : "none"],
          ]}
        />
      );
    case "plan":
      return (
        <Fields
          rows={[
            ["search text", d.query],
            ["English", d.english ?? undefined],
            ["language", d.language],
            ["type", d.query_type],
            ["identifiers", d.identifiers],
            ["hard question", d.hard],
            ["research loop", d.needs_research],
            ["filters", Object.keys(d.filters ?? {}).length ? d.filters : "none"],
            ["boosts", Object.keys(d.boosts ?? {}).length ? d.boosts : "none"],
            ["planned by", d.planned_by === "llm" ? "rules + router model" : "rules"],
            ["answer model", `${d.answer_model} (${d.answer_role})`],
            ["reasons", d.reasons?.join("\n")],
          ]}
        />
      );
    case "embed":
      return (
        <Fields
          rows={[
            ["text", d.text],
            ["model", `${d.provider}/${d.model}`],
            ["from", d.source === "model" ? "computed by the model now" : d.source],
            ["dimensions", d.dims],
            ["length (norm)", d.norm],
            ["first values", `[${(d.preview ?? []).map((v: number) => v.toFixed(5)).join(", ")}, …]`],
          ]}
        />
      );
    case "search":
      return <SearchDetails d={d} />;
    case "llm":
      return <ModelCallDetails d={d} item={item} />;
    case "named":
      return <Table head={["file", "share of its name in the question"]} rows={(d.files ?? []).map((f: any) => [f.file, num(f.coverage, 2)])} />;
    case "strategy":
      return (
        <>
          <Fields
            rows={[
              ["strategy", d.strategy],
              ["chosen by", d.chosen_by === "model" ? "the planner model" : "rules"],
              ["files to read", d.files?.length ? d.files : undefined],
              ["subject", d.subject || undefined],
              ["file-name words", d.name_words?.length ? d.name_words : undefined],
              ["parts", d.sub_questions?.length ? d.sub_questions.join("\n") : undefined],
            ]}
          />
          <Section title="Documents the planner saw">
            <ol className="list-decimal space-y-0.5 pl-5 text-[12px]">
              {(d.candidates ?? []).map((c: any, i: number) => (
                <li key={i}>
                  {c.file}
                  {c.named ? <span className="ml-1 text-accent">named</span> : null}
                </li>
              ))}
            </ol>
          </Section>
        </>
      );
    case "read":
      return (
        <>
          <Fields rows={[["why", d.why], ["budget", d.budget_chars ? `${d.budget_chars.toLocaleString()} chars, ${d.per_document?.toLocaleString()} per document` : undefined]]} />
          <Table
            head={["file", "chunks read", "characters", "complete"]}
            rows={(d.documents ?? []).map((x: any) => [x.file, x.chunks_total ? `${x.chunks} of ${x.chunks_total}` : x.chunks, num(x.chars), x.cut ? <span className="text-warning">cut at the budget</span> : "yes"])}
          />
        </>
      );
    case "candidates":
      return (
        <>
          <Fields rows={[["subject stems", d.stems], ["limit", d.limit], ["read of each", `${num(d.chars_per_document)} characters`]]} />
          <Table head={["#", "file", "why"]} rows={(d.documents ?? []).map((x: any, i: number) => [i + 1, x.file, x.why])} />
        </>
      );
    case "verdicts":
      return (
        <Table
          head={["file", "batch", "relevant", "pages", "note"]}
          rows={(d.documents ?? []).map((x: any) => [
            x.file,
            x.batch,
            x.relevant === true ? <span className="text-success">yes</span> : x.relevant === false ? "no" : <span className="text-warning">unknown</span>,
            x.pages?.join(", ") || "—",
            x.note,
          ])}
        />
      );
    case "evidence":
      return d.passages ? (
        <div className="space-y-1">
          {d.passages.map((p: any) => (
            <TextBlock key={p.n} text={p.text} label={`[${p.n}] ${p.file} · pp.${p.pages?.join("-")} · from ${p.from}`} />
          ))}
        </div>
      ) : (
        <Fields rows={[["off topic", d.off_topic], ["signals", d.signals]]} />
      );
    case "tool":
      return (
        <>
          <Fields rows={[["tool", d.tool], ["arguments", d.arguments]]} />
          <TextBlock text={d.output ?? ""} label="what the tool returned" open />
        </>
      );
    case "check":
      return <Fields rows={[["passages checked", d.passages], ["complete", d.complete], ["missing", d.missing || undefined]]} />;
    case "calc":
      return (
        <>
          <Fields rows={[["how", d.how]]} />
          <ul className="list-disc space-y-0.5 pl-5 font-mono text-[12px]">
            {(d.sums ?? []).map((s: string) => (
              <li key={s}>{s}</li>
            ))}
          </ul>
        </>
      );
    case "citations":
      return (
        <>
          <Fields rows={[["support (groundedness)", d.groundedness], ["judged by", d.judge], ["sentences without a citation", d.uncited_claims]]} />
          <Table
            head={["#", "file · page", "verified", "quote", "numbers not on the page"]}
            rows={(d.citations ?? []).map((c: any) => [
              c.n,
              `${c.file} · p.${c.page}`,
              c.verified ? <span className="text-success">yes</span> : <span className="text-warning">no</span>,
              c.quote,
              c.missing_numbers?.join(", ") || "—",
            ])}
          />
        </>
      );
    case "done":
      return (
        <Fields
          rows={[
            ["status", d.answer_status],
            ["model", d.model],
            ["tokens", d.usage ? `${compact(d.usage.input_tokens)} in · ${compact(d.usage.output_tokens)} out` : undefined],
            ["cost", d.usage?.cost_usd != null ? usd(d.usage.cost_usd) : undefined],
            ["time per step", Object.entries(d.timings_ms ?? {}).map(([k, v]) => `${k} ${ms(v as number)}`).join("\n")],
          ]}
        />
      );
    default:
      return <Fields rows={Object.entries(d)} />;
  }
}

/* ---- the panel ----------------------------------------------------------------------------------------------------- */

function Step({ item, open, onToggle }: { item: TraceItem; open: boolean; onToggle: () => void }) {
  const kind = KINDS[item.kind] ?? { icon: Sparkles, group: "all", label: item.kind };
  const Icon = kind.icon;
  const d = item.data ?? {};
  const meta =
    item.kind === "llm" && d.input_tokens != null
      ? `${compact(d.input_tokens)} in · ${compact(d.output_tokens)} out${d.cost_usd ? ` · ${usd(d.cost_usd)}` : ""}`
      : null;
  return (
    <li className={clsx("rounded-xl border", open ? "border-border-strong" : "border-border")}>
      <button type="button" onClick={onToggle} aria-expanded={open} className="flex w-full items-start gap-2.5 px-3 py-2 text-left hover:bg-surface-hover/60">
        <span className={clsx("mt-0.5 shrink-0", item.status === "error" ? "text-danger" : item.kind === "llm" ? "text-accent" : "text-muted")}>
          {item.status === "running" ? <Loader2 size={14} className="animate-spin" aria-hidden /> : item.status === "error" ? <AlertTriangle size={14} aria-hidden /> : <Icon size={14} aria-hidden />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block text-[12.5px] leading-[1.4]">{item.title}</span>
          <span className="mt-0.5 flex flex-wrap gap-x-2 text-[11px] text-muted">
            <span>{kind.label}</span>
            <span className="tabular">at {ms(item.at_ms)}</span>
            {item.ms != null ? <span className="tabular">took {ms(item.ms)}</span> : null}
            {meta ? <span className="tabular">{meta}</span> : null}
          </span>
        </span>
        <ChevronRight size={14} className={clsx("mt-0.5 shrink-0 text-muted transition-transform", open && "rotate-90")} aria-hidden />
      </button>
      {open ? (
        <div className="space-y-3 border-t border-border px-3 py-3">
          <Details item={item} />
        </div>
      ) : null}
    </li>
  );
}

function PanelBody({ message, question, onClose }: { message: ChatMessage; question?: string; onClose: () => void }) {
  const toast = useToast();
  const items = useMemo(() => [...(message.trace ?? [])].sort((a, b) => a.seq - b.seq), [message.trace]);
  const [group, setGroup] = useState<Group>("all");
  const [open, setOpen] = useState<Set<string>>(new Set());
  const listRef = useRef<HTMLDivElement>(null);
  const follow = useRef(true);
  const live = Boolean(message.streaming);

  const calls = items.filter((i) => i.kind === "llm");
  const totals = calls.reduce(
    (t, c) => ({ inT: t.inT + (c.data.input_tokens ?? 0), outT: t.outT + (c.data.output_tokens ?? 0), cost: t.cost + (c.data.cost_usd ?? 0) }),
    { inT: 0, outT: 0, cost: 0 },
  );
  const running = calls.filter((c) => c.status === "running").length;
  const searches = items.filter((i) => i.kind === "search").length;
  const shown = group === "all" ? items : items.filter((i) => (KINDS[i.kind]?.group ?? "all") === group);
  const elapsed = items.length ? Math.max(...items.map((i) => i.at_ms + (i.ms ?? 0))) : 0;

  useEffect(() => {
    const el = listRef.current;
    if (el && live && follow.current) el.scrollTop = el.scrollHeight;
  }, [items, live]);

  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const json = () => JSON.stringify({ question, items }, null, 2);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(json());
      toast("Details copied as JSON");
    } catch {
      toast("Copying needs clipboard permission in this browser", "error");
    }
  };
  const download = () => {
    const url = URL.createObjectURL(new Blob([json()], { type: "application/json" }));
    const a = document.createElement("a");
    a.href = url;
    a.download = `answer-details-${new Date().toISOString().slice(0, 19).replace(/[:T]/g, "-")}.json`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <header className="shrink-0 space-y-2 border-b border-border px-4 py-3">
        <div className="flex items-center gap-2">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2 text-[14px] font-semibold">
              Answer details
              {live ? (
                <span className="inline-flex items-center gap-1 rounded-full bg-accent-soft px-2 py-px text-[11px] font-medium text-accent">
                  <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent" aria-hidden /> live
                </span>
              ) : null}
            </div>
            {question ? <div className="mt-0.5 line-clamp-2 text-[12px] text-muted">{question}</div> : null}
          </div>
          <button type="button" onClick={copy} className="rounded-lg p-1.5 text-muted hover:bg-surface-hover hover:text-foreground" title="Copy everything as JSON" aria-label="Copy everything as JSON">
            <Copy size={15} />
          </button>
          <button type="button" onClick={download} className="rounded-lg p-1.5 text-muted hover:bg-surface-hover hover:text-foreground" title="Download as JSON" aria-label="Download as JSON">
            <Download size={15} />
          </button>
          <button type="button" onClick={onClose} className="rounded-lg p-1.5 text-muted hover:bg-surface-hover hover:text-foreground" title="Close" aria-label="Close answer details">
            <X size={16} />
          </button>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[12px] text-muted">
          <span className="tabular">
            {calls.length} model call{calls.length === 1 ? "" : "s"}
            {running ? ` (${running} running)` : ""}
          </span>
          <span className="tabular">
            {compact(totals.inT)} in · {compact(totals.outT)} out tokens
          </span>
          <span className="tabular">{usd(totals.cost)}</span>
          <span className="tabular">
            {searches} search{searches === 1 ? "" : "es"}
          </span>
          <span className="tabular">{ms(elapsed)}</span>
        </div>
        <div className="flex flex-wrap items-center gap-1">
          {GROUPS.map((g) => (
            <button
              key={g.id}
              type="button"
              onClick={() => setGroup(g.id)}
              className={clsx(
                "rounded-full border px-2.5 py-0.5 text-[12px]",
                group === g.id ? "border-foreground/40 bg-surface-hover text-foreground" : "border-border text-muted hover:text-foreground",
              )}
            >
              {g.label}
            </button>
          ))}
          <span className="ml-auto flex gap-1 text-[12px]">
            <button type="button" onClick={() => setOpen(new Set(shown.map((i) => i.id)))} className="rounded-md px-1.5 py-0.5 text-muted hover:text-foreground">
              Open all
            </button>
            <button type="button" onClick={() => setOpen(new Set())} className="rounded-md px-1.5 py-0.5 text-muted hover:text-foreground">
              Close all
            </button>
          </span>
        </div>
      </header>
      <div
        ref={listRef}
        onScroll={() => {
          const el = listRef.current;
          if (el) follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
        }}
        className="min-h-0 flex-1 overflow-y-auto px-4 py-3"
      >
        {items.length ? (
          <ol className="space-y-2">
            {shown.map((item) => (
              <Step key={item.id} item={item} open={open.has(item.id)} onToggle={() => toggle(item.id)} />
            ))}
          </ol>
        ) : (
          <p className="text-[13px] text-muted">
            {live ? "Waiting for the first step…" : "No details were recorded for this answer. They exist only for answers asked in this tab since it was opened."}
          </p>
        )}
      </div>
    </>
  );
}

/** Right-hand panel with every step of one answer: what was searched, scored, read and sent to which model, and what came back. */
export function DetailsPanel({ message, question, onClose }: { message: ChatMessage; question?: string; onClose: () => void }) {
  const close = useCallback(() => onClose(), [onClose]);
  useEscape(close, true);
  return (
    <>
      <aside className="hidden h-full min-h-0 w-[min(640px,50vw)] shrink-0 flex-col border-l border-border bg-background lg:flex" aria-label="Answer details">
        <PanelBody message={message} question={question} onClose={onClose} />
      </aside>
      <div className="lg:hidden">
        <div className="fixed inset-0 z-30 bg-black/30" onClick={onClose} aria-hidden />
        <aside className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[640px] flex-col border-l border-border bg-background shadow-panel" role="dialog" aria-modal aria-label="Answer details">
          <PanelBody message={message} question={question} onClose={onClose} />
        </aside>
      </div>
    </>
  );
}
