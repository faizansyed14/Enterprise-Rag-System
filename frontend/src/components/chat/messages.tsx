"use client";

import clsx from "clsx";
import {
  AlertTriangle,
  Check,
  CircleCheck,
  Copy,
  FileSearch,
  RotateCcw,
  ShieldAlert,
  ThumbsDown,
  ThumbsUp,
  Waypoints,
} from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ModelIcon, modelLabel, splitSpec } from "@/components/brand/brand";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { docTypeLabel, ms, pct } from "@/lib/format";
import type { ChatMessage, Citation, PlanInfo } from "@/lib/types";

import { AnswerText, citationOrder, plainAnswer } from "./answer-text";
import { SourceCard } from "./source-card";

export function UserMessage({ message }: { message: ChatMessage }) {
  return (
    <div className="flex justify-end">
      <div dir="auto" className="max-w-[85%] whitespace-pre-wrap rounded-2xl rounded-br-md bg-bubble-user px-4 py-2.5 text-[15px] leading-relaxed text-bubble-user-fg">
        {message.content}
      </div>
    </div>
  );
}

function PlanLine({ plan }: { plan: PlanInfo }) {
  const chips: string[] = [];
  for (const id of plan.identifiers ?? []) chips.push(id);
  for (const p of plan.project_ids ?? []) chips.push(p);
  for (const t of plan.doc_types ?? []) chips.push(docTypeLabel(t));
  const lead =
    plan.query_type === "identifier" ? "Looked up" : plan.hard ? "Researched across documents" : plan.language && plan.language !== "en" ? "Searched in both languages" : "Searched";
  if (!chips.length && lead === "Searched") return null;
  return (
    <div className="mb-3 flex flex-wrap items-center gap-1.5 text-[12px] text-muted">
      <FileSearch size={13} aria-hidden />
      <span>{lead}</span>
      {chips.map((chip, i) => (
        <span key={`${chip}-${i}`} className="rounded-md border border-border px-1.5 py-px tabular">
          {chip}
        </span>
      ))}
    </div>
  );
}

function Progress({ message }: { message: ChatMessage }) {
  return (
    <div className="space-y-1.5" aria-live="polite">
      <div className="flex items-center gap-2 text-[13.5px] text-muted">
        <span className="pulse-dot inline-block h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
        {message.stageText || "Working"}
      </div>
      {message.steps?.length ? (
        <ol className="ml-[3px] space-y-1 border-l border-border pl-4 text-[12.5px] text-muted">
          {message.steps.map((step, i) => (
            <li key={i}>{step}</li>
          ))}
        </ol>
      ) : null}
    </div>
  );
}

function NotFound({ message }: { message: ChatMessage }) {
  const documents = message.done?.documents ?? [];
  return (
    <div className="mt-4 rounded-xl border border-border px-4 py-3">
      <div className="text-[13px] font-medium">Not answered by the documents you can open</div>
      <p className="mt-1 text-[12.5px] text-muted">Nothing was guessed. Try naming the document or the project, or ask an administrator whether the source is connected.</p>
      {documents.length ? (
        <div className="mt-3">
          <div className="text-[12px] text-muted">{message.done?.closest_label || "Closest documents"}</div>
          <ul className="mt-1.5 space-y-1">
            {documents.map((d) => (
              <li key={d.document_id} className="flex items-center gap-2 text-[13px]">
                <Link href={`/documents/${d.document_id}`} className="min-w-0 truncate underline-offset-2 hover:underline">
                  <span className="font-medium tabular">{d.doc_number || d.file_name}</span>
                  <span className="text-muted"> {d.title}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </div>
  );
}

function Sources({
  citations,
  order,
  activeKey,
  onOpen,
}: {
  citations: Citation[];
  order: Map<number, number>;
  activeKey?: string | null;
  onOpen: (citation: Citation, display: number) => void;
}) {
  const [all, setAll] = useState(false);
  const sorted = [...citations].sort((a, b) => (order.get(a.n) ?? 999) - (order.get(b.n) ?? 999));
  const shown = all ? sorted : sorted.slice(0, 3);
  return (
    <div className="mt-5">
      <div className="mb-2 text-[12.5px] font-medium text-muted">Sources</div>
      <div className="space-y-2">
        {shown.map((c) => {
          const display = order.get(c.n) ?? c.n;
          const key = `${c.document_id}:${c.version_id}:${c.page}:${c.n}`;
          return (
            <SourceCard
              key={c.n}
              citation={c}
              display={display}
              active={activeKey === key}
              onSelect={onOpen}
            />
          );
        })}
      </div>
      {sorted.length > 3 ? (
        <button type="button" onClick={() => setAll(!all)} className="mt-2 rounded-md px-1.5 py-1 text-[12.5px] text-muted hover:bg-surface-hover hover:text-foreground">
          {all ? "Show fewer sources" : `Show all ${sorted.length} sources`}
        </button>
      ) : null}
    </div>
  );
}

function Footer({ message, order, isAdmin, showBadge }: { message: ChatMessage; order: Map<number, number>; isAdmin: boolean; showBadge: boolean }) {
  const toast = useToast();
  const [score, setScore] = useState<number>(message.feedback_score ?? 0);
  const [copied, setCopied] = useState(false);
  const verified = message.citations.filter((c) => c.verified).length;
  const total = message.citations.length;
  const model = message.model ? splitSpec(message.model) : null;

  const copy = async () => {
    const sources = [...message.citations]
      .sort((a, b) => (order.get(a.n) ?? 999) - (order.get(b.n) ?? 999))
      .map((c) => `[${order.get(c.n) ?? c.n}] ${c.doc_number || c.file_name}, ${c.title}, p. ${c.page}`);
    const text = plainAnswer(message.content, order) + (sources.length ? `\n\nSources\n${sources.join("\n")}` : "");
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      toast("Copying needs clipboard permission in this browser", "error");
    }
  };
  const rate = async (value: number) => {
    if (!message.id) return;
    const next = score === value ? 0 : value;
    setScore(next);
    try {
      await api(`/messages/${message.id}/feedback`, { body: { score: next } });
      if (next) toast(next > 0 ? "Thanks, marked as helpful" : "Thanks, marked as not helpful");
    } catch (e) {
      setScore(score);
      toast(errorMessage(e), "error");
    }
  };

  return (
    <div className="mt-4 flex flex-wrap items-center gap-x-4 gap-y-2 text-[12px] text-muted">
      {total ? (
        <span className={clsx("inline-flex items-center gap-1", verified === total ? "text-success" : "text-warning")} title="Quotes found on the cited pages">
          {verified === total ? <CircleCheck size={13} aria-hidden /> : <AlertTriangle size={13} aria-hidden />}
          {verified} of {total} quotes found
        </span>
      ) : null}
      {message.groundedness !== null && message.groundedness !== undefined && total ? (
        <span title="Share of the answer supported by the cited passages, from the groundedness check">Support {pct(message.groundedness)}</span>
      ) : null}
      {showBadge && model ? (
        <span className="inline-flex items-center gap-1.5" title={message.model ?? undefined}>
          <ModelIcon provider={model.provider} model={model.model} size={13} tile={false} />
          {modelLabel(model.provider, model.model)}
        </span>
      ) : null}
      {message.done?.timings_ms?.total ? <span className="tabular">{ms(message.done.timings_ms.total)}</span> : null}
      <span className="ml-auto flex items-center gap-0.5">
        <button type="button" onClick={copy} className="rounded-md p-1.5 hover:bg-surface-hover hover:text-foreground" aria-label="Copy answer" title="Copy answer with sources">
          {copied ? <Check size={14} /> : <Copy size={14} />}
        </button>
        {message.id ? (
          <>
            <button type="button" onClick={() => rate(1)} aria-pressed={score > 0} className={clsx("rounded-md p-1.5 hover:bg-surface-hover hover:text-foreground", score > 0 && "text-foreground")} aria-label="Helpful" title="Helpful">
              <ThumbsUp size={14} fill={score > 0 ? "currentColor" : "none"} />
            </button>
            <button type="button" onClick={() => rate(-1)} aria-pressed={score < 0} className={clsx("rounded-md p-1.5 hover:bg-surface-hover hover:text-foreground", score < 0 && "text-foreground")} aria-label="Not helpful" title="Not helpful">
              <ThumbsDown size={14} fill={score < 0 ? "currentColor" : "none"} />
            </button>
          </>
        ) : null}
        {isAdmin && message.trace_id ? (
          <Link href={`/monitor?trace=${message.trace_id}`} className="rounded-md p-1.5 hover:bg-surface-hover hover:text-foreground" aria-label="Open the trace for this answer" title="Trace">
            <Waypoints size={14} />
          </Link>
        ) : null}
      </span>
    </div>
  );
}

export function AssistantMessage({
  message,
  onOpen,
  activeSourceKey,
  isAdmin,
  showBadge,
  onRetry,
}: {
  message: ChatMessage;
  onOpen: (citation: Citation, display: number, messageKey: string) => void;
  activeSourceKey?: string | null;
  isAdmin: boolean;
  showBadge: boolean;
  onRetry?: () => void;
}) {
  const order = citationOrder(message.content);
  const status = message.done?.status ?? message.status;
  const open = (c: Citation, display?: number) => onOpen(c, display ?? order.get(c.n) ?? c.n, message.key);
  const refused = message.errorCode === "guardrail" || status === "refused";

  return (
    <article className="min-w-0">
      {message.plan ? <PlanLine plan={message.plan} /> : null}
      {message.streaming && !message.content ? <Progress message={message} /> : null}
      {message.content ? <AnswerText text={message.content} citations={message.citations} onOpen={(c) => open(c)} streaming={message.streaming} /> : null}
      {refused ? (
        <div className="flex items-start gap-2 rounded-xl border border-border px-4 py-3 text-[13.5px]">
          <ShieldAlert size={16} className="mt-0.5 shrink-0 text-muted" aria-hidden />
          <div>
            <div className="font-medium">This question was not sent</div>
            <div className="mt-0.5 text-muted">{message.error || message.content || "It looks like an instruction to the system rather than a question about the documents."}</div>
          </div>
        </div>
      ) : message.error || status === "error" ? (
        <div role="alert" className="flex flex-wrap items-center gap-3 rounded-xl border border-danger/30 bg-danger-soft px-4 py-3 text-[13.5px] text-danger">
          <AlertTriangle size={16} className="shrink-0" aria-hidden />
          <span className="min-w-0 flex-1">{message.error || "The answer could not be completed."}</span>
          {onRetry ? (
            <button type="button" onClick={onRetry} className="inline-flex items-center gap-1.5 rounded-lg border border-danger/40 px-2.5 py-1 text-[12.5px] font-medium hover:bg-danger/10">
              <RotateCcw size={13} aria-hidden />
              Try again
            </button>
          ) : null}
        </div>
      ) : null}
      {!message.streaming && status === "not_found" ? <NotFound message={message} /> : null}
      {!message.streaming && status === "unverified" && message.citations.length ? (
        <div className="mt-4 flex items-start gap-2 rounded-xl border border-warning/40 bg-warning-soft px-4 py-3 text-[13px] text-warning">
          <AlertTriangle size={15} className="mt-0.5 shrink-0" aria-hidden />
          <span>The quotes could not be matched to the cited pages. Open the sources and check them before relying on this answer.</span>
        </div>
      ) : null}
      {message.citations.length ? (
        <Sources citations={message.citations} order={order} activeKey={activeSourceKey} onOpen={open} />
      ) : null}
      {!message.streaming && message.content && !refused ? <Footer message={message} order={order} isAdmin={isAdmin} showBadge={showBadge} /> : null}
    </article>
  );
}
