"use client";

import clsx from "clsx";
import { AlertTriangle, BookOpenText, BrainCircuit, Check, ChevronDown, FileCheck2, PenLine, Search } from "lucide-react";
import { useState } from "react";

import { ms } from "@/lib/format";
import type { ChatMessage } from "@/lib/types";

const labels: Record<string, string> = {
  plan: "Understanding the question",
  search: "Searching project documents",
  research: "Reading the evidence",
  write: "Writing the answer",
  check: "Checking the evidence",
  verify: "Checking citations",
};

function StageIcon({ stage }: { stage: string }) {
  const props = { size: 14, strokeWidth: 1.8, "aria-hidden": true as const };
  if (stage === "plan") return <BrainCircuit {...props} />;
  if (stage === "search") return <Search {...props} />;
  if (stage === "research") return <BookOpenText {...props} />;
  if (stage === "verify") return <FileCheck2 {...props} />;
  return <PenLine {...props} />;
}

function buildRows(message: ChatMessage) {
  const rows = [...(message.activities ?? [])];
  if (!message.streaming && message.done?.timings_ms?.verify !== undefined) {
    rows.push({ stage: "verify", message: "Checked citations against the source pages", at: 0 });
  }
  return rows;
}

export function ActivityTrail({ message }: { message: ChatMessage }) {
  const [open, setOpen] = useState(false);
  const rows = buildRows(message);
  if (!rows.length) return null;

  const failed = Boolean(message.error || message.status === "error" || message.done?.status === "error");
  const stopped = message.error === "Stopped.";
  const finished = !message.streaming;
  const expanded = Boolean(message.streaming || open);
  const current = rows.at(-1)!;
  const totalMs = message.done?.timings_ms?.total;

  const summaryLive = message.streaming
    ? current.message || labels[current.stage] || "In progress"
    : failed
      ? stopped
        ? `Interrupted after ${rows.length} answer steps`
        : `Failed after ${rows.length} answer steps`
      : `Completed ${rows.length} answer steps`;

  return (
    <section className="activity-trail mb-4" aria-label="Answer activity">
      <button
        type="button"
        onClick={() => finished && setOpen((value) => !value)}
        aria-expanded={expanded}
        disabled={message.streaming}
        className={clsx(
          "activity-summary flex w-full items-center gap-2.5 rounded-lg py-1 text-left text-[13px] text-muted transition-colors",
          finished && "hover:bg-surface-hover/60 hover:text-foreground",
          message.streaming && "cursor-default",
        )}
      >
        <span
          className={clsx(
            "activity-symbol",
            message.streaming && `activity-${current.stage}`,
            finished && failed && "text-danger",
          )}
          aria-hidden
        >
          {finished && failed ? <AlertTriangle size={14} strokeWidth={1.8} /> : <StageIcon stage={current.stage} />}
        </span>
        <span
          className="min-w-0 flex-1 truncate font-medium text-foreground/85"
          aria-live={message.streaming ? "polite" : "off"}
        >
          {summaryLive}
          {!message.streaming && totalMs ? <span className="font-normal text-muted"> · {ms(totalMs)}</span> : null}
        </span>
        {message.streaming ? (
          <span className="activity-working shrink-0 text-[12px] text-muted">In progress</span>
        ) : null}
        <ChevronDown
          size={14}
          className={clsx("shrink-0 transition-transform duration-200", expanded && "rotate-180")}
          aria-hidden
        />
      </button>
      {expanded ? (
        <ol className="activity-list ml-[7px] mt-1.5 space-y-0.5 border-l border-border pl-5">
          {rows.map((item, index) => {
            const active = Boolean(message.streaming && index === rows.length - 1);
            const rowFailed = Boolean(finished && failed && index === rows.length - 1);
            return (
              <li
                key={`${item.stage}:${item.message}:${index}`}
                className={clsx(
                  "activity-row relative flex min-h-7 items-start gap-2 py-1 text-[12.5px] transition-colors duration-200",
                  active && "text-foreground",
                  rowFailed && "text-danger",
                  !active && !rowFailed && "text-muted",
                )}
              >
                <span
                  className={clsx(
                    "activity-node absolute -left-[25px] top-[7px]",
                    active && "is-active",
                    rowFailed && "is-failed",
                  )}
                  aria-hidden
                >
                  {active ? (
                    <span />
                  ) : rowFailed ? (
                    <AlertTriangle size={9} strokeWidth={2.4} />
                  ) : (
                    <Check size={9} strokeWidth={2.6} />
                  )}
                </span>
                <span className={clsx("mt-[2px] shrink-0", active && `activity-${item.stage}`)}>
                  <StageIcon stage={item.stage} />
                </span>
                <span className="min-w-0 leading-[1.45]">{item.message || labels[item.stage]}</span>
              </li>
            );
          })}
        </ol>
      ) : null}
    </section>
  );
}
