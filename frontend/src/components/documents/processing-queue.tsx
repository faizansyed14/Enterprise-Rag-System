"use client";

import { ChevronDown, CircleAlert, Clock, LoaderCircle } from "lucide-react";
import { useEffect, useRef, useState } from "react";

import { Button } from "@/components/ui/button";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/lib/hooks";

type Item = {
  id: string;
  name: string;
  source: string;
  state: "running" | "waiting" | "failed";
  step: string;
  seconds: number | null;
  chunks: number | null;
  pages: number | null;
  retry_jobs: number[];
};
type Queue = { items: Item[]; running: number; waiting: number; failed: number; jobs_ahead: number };

function elapsed(seconds: number | null): string {
  if (seconds === null) return "";
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

/** Files that are not searchable yet: what each one is doing now, how long, and why a failed one failed. */
export function ProcessingQueue({ onFinished }: { onFinished: () => void }) {
  const { data, reload } = useApi<Queue>("/documents/processing", { refreshMs: 4000 });
  const [open, setOpen] = useState(false);
  const toast = useToast();
  const busyBefore = useRef(0);
  const busy = (data?.running ?? 0) + (data?.waiting ?? 0);

  // when the last waiting or running file finishes, the document list is refreshed
  useEffect(() => {
    if (busyBefore.current > 0 && busy === 0) onFinished();
    busyBefore.current = busy;
  }, [busy, onFinished]);

  if (!data || !data.items.length) return null;
  const retry = async (ids: number[]) => {
    try {
      const r = await api<{ retried: number }>("/documents/processing/retry", { body: { ids } });
      toast(r.retried ? "Queued again" : "Nothing to retry");
      void reload();
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  };
  const parts = [
    data.running ? `${data.running} processing` : "",
    data.waiting ? `${data.waiting} waiting` : "",
    data.failed ? `${data.failed} failed` : "",
  ].filter(Boolean);
  return (
    <div className="mx-6 mt-4 rounded-2xl border border-border lg:mx-10">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-[13px] hover:bg-surface-hover"
      >
        {busy ? <LoaderCircle size={15} className="animate-spin text-muted motion-reduce:animate-none" aria-hidden /> : <CircleAlert size={15} className="text-danger" aria-hidden />}
        <span className="flex-1">
          <span className="font-medium">{parts.join(" · ")}</span>
          <span className="text-muted"> — not searchable yet</span>
        </span>
        <ChevronDown size={15} className={`text-muted transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open ? (
        <ul className="divide-y divide-border border-t border-border">
          {data.items.map((item) => (
            <li key={item.id} className="flex items-start gap-3 px-4 py-2.5 text-[13px]">
              {item.state === "running" ? (
                <LoaderCircle size={15} className="mt-0.5 shrink-0 animate-spin text-muted motion-reduce:animate-none" aria-label="Processing" />
              ) : item.state === "waiting" ? (
                <Clock size={15} className="mt-0.5 shrink-0 text-muted" aria-label="Waiting" />
              ) : (
                <CircleAlert size={15} className="mt-0.5 shrink-0 text-danger" aria-label="Failed" />
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate font-medium">{item.name}</span>
                <span className={`block text-[12px] ${item.state === "failed" ? "text-danger" : "text-muted"}`}>
                  {item.step}
                  {item.seconds !== null ? ` · ${elapsed(item.seconds)}` : ""}
                  {item.chunks ? ` · ${item.chunks} chunks` : ""}
                </span>
              </span>
              {item.state === "failed" && item.retry_jobs.length ? (
                <Button size="sm" onClick={() => void retry(item.retry_jobs)}>
                  Retry
                </Button>
              ) : null}
            </li>
          ))}
          {data.jobs_ahead > 0 ? (
            <li className="px-4 py-2 text-[12px] text-muted">{data.jobs_ahead} steps are waiting in the queue across all files.</li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}
