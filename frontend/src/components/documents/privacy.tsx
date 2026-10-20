"use client";

import { ShieldAlert, ShieldCheck } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/overlay";
import { useApi } from "@/lib/hooks";

/** What the server reports about sensitive values (bank accounts, IBANs, cards, ids) in a file that has just been read. */
export type PrivacyNote = { phrase: string; found: Record<string, number>; shield: boolean; held_back: boolean; redacted: boolean };

type PageView = { page: number; found: Record<string, number>; model_view: string };
type Report = {
  phrase: string;
  found: Record<string, number>;
  chunks: number;
  pages: PageView[];
  shield: boolean;
  local_only: boolean;
  auto_local_only: boolean;
  redacted_in_storage: boolean;
};

const HIDDEN = "replaced by placeholders before any cloud model sees them; the answer you read still shows the real value";

/** One line under an uploaded file: what was found and that it stays away from cloud models. */
export function PrivacyLine({ note }: { note: PrivacyNote }) {
  const count = Object.values(note.found).reduce((a, b) => a + b, 0);
  if (!count) {
    return (
      <span className="mt-0.5 flex items-center gap-1 text-[12px] text-muted">
        <ShieldCheck size={12} aria-hidden /> No bank, card or ID numbers found
      </span>
    );
  }
  if (!note.shield) {
    return (
      <span className="mt-0.5 flex items-start gap-1 text-[12px] text-warning">
        <ShieldAlert size={12} className="mt-0.5 shrink-0" aria-hidden /> Found {note.phrase}. The privacy shield is off, so these can reach cloud models.
      </span>
    );
  }
  return (
    <span className="mt-0.5 flex items-start gap-1 text-[12px] text-success">
      <ShieldCheck size={12} className="mt-0.5 shrink-0" aria-hidden />
      <span>
        Found {note.phrase}: {note.redacted ? "removed from the stored text." : `${HIDDEN}.`}
        {note.held_back ? " This document is also kept local-only." : ""}
      </span>
    </span>
  );
}

/** On the document page: what was found, and a view of exactly what a cloud model receives in its place. */
export function PrivacyBanner({ documentId, versionId }: { documentId: string; versionId: string }) {
  const { data } = useApi<Report>(`/documents/${documentId}/privacy?${new URLSearchParams({ version: versionId })}`);
  const [open, setOpen] = useState(false);
  if (!data) return null;
  const count = Object.values(data.found).reduce((a, b) => a + b, 0);
  const tone = !data.shield && count ? "text-warning" : count ? "text-success" : "text-muted";
  return (
    <div className="border-b border-border px-4 py-2 sm:px-6 lg:px-8">
      <div className={`flex flex-wrap items-center gap-x-3 gap-y-1 text-[12.5px] ${tone}`}>
        {!data.shield && count ? <ShieldAlert size={14} aria-hidden /> : <ShieldCheck size={14} aria-hidden />}
        <span className="min-w-0 flex-1">
          {!count
            ? "No bank, card or ID numbers found in this document."
            : !data.shield
              ? `Found ${data.phrase}. The privacy shield is off, so these can reach cloud models.`
              : `Found ${data.phrase} (pages ${data.pages.map((p) => p.page).join(", ")}). They are ${data.redacted_in_storage ? "removed from the stored text" : HIDDEN}.`}
          {data.local_only ? " This document is local-only: nothing from it goes to a cloud model." : ""}
          {count ? <span className="text-muted"> The page below is your own stored copy.</span> : null}
        </span>
        {count ? (
          <Button size="sm" variant="ghost" onClick={() => setOpen(true)}>
            What a model receives
          </Button>
        ) : null}
      </div>
      <Dialog open={open} onClose={() => setOpen(false)} wide title="What a cloud model receives from this document" footer={<Button onClick={() => setOpen(false)}>Close</Button>}>
        <p className="text-[13px] text-muted">
          These are the passages that contain {data.phrase}, exactly as they are sent. The real values stay on your server and are put back into the
          answer you read. Passages without such values are sent unchanged.
        </p>
        <div className="space-y-3">
          {data.pages.map((p) => (
            <div key={p.page} className="rounded-xl border border-border">
              <div className="border-b border-border px-3 py-1.5 text-[12px] text-muted">Page {p.page}</div>
              <pre className="max-h-64 overflow-auto whitespace-pre-wrap px-3 py-2 text-[12.5px] leading-5">{p.model_view}</pre>
            </div>
          ))}
        </div>
      </Dialog>
    </div>
  );
}
