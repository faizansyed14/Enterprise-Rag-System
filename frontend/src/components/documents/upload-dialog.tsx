"use client";

import clsx from "clsx";
import { CircleCheck, CircleX, FileUp, LoaderCircle, X } from "lucide-react";
import Link from "next/link";
import { useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/fields";
import { Dialog } from "@/components/ui/overlay";
import { PrivacyLine, type PrivacyNote } from "@/components/documents/privacy";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import type { Facets } from "@/lib/types";

type Row = { key: string; file: File; state: "ready" | "sending" | "processing" | "done" | "failed"; detail?: string; itemId?: string; documentId?: string | null; privacy?: PrivacyNote | null };

const ACCEPT = ".pdf,.docx,.doc,.rtf,.odt,.xlsx,.xlsm,.xls,.ods,.pptx,.ppt,.odp,.msg,.eml,.txt,.md,.csv,.tsv,.json,.xml,.html,.htm,.png,.jpg,.jpeg,.tif,.tiff,.bmp,.webp,.gif,.zip";

function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function ProjectPicker({
  value,
  onChange,
  projects,
}: {
  value: string;
  onChange: (value: string) => void;
  projects: string[];
}) {
  const [open, setOpen] = useState(false);
  const [highlight, setHighlight] = useState(0);
  const wrap = useRef<HTMLDivElement>(null);
  const listId = "upload-project-suggestions";

  const matches = useMemo(() => {
    const q = value.trim().toLowerCase();
    const list = q ? projects.filter((p) => p.toLowerCase().includes(q)) : projects;
    return list.slice(0, 12);
  }, [projects, value]);

  const exact = projects.some((p) => p.toLowerCase() === value.trim().toLowerCase());
  const show = Boolean(open && (matches.length > 0 || (value.trim() && !exact)));

  useEffect(() => {
    setHighlight(0);
  }, [value, open]);

  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const pick = (name: string) => {
    onChange(name);
    setOpen(false);
  };

  const onKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (!show) {
      if (event.key === "ArrowDown" && matches.length) setOpen(true);
      return;
    }
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setHighlight((i) => Math.min(i + 1, Math.max(0, matches.length - 1)));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setHighlight((i) => Math.max(i - 1, 0));
    } else if (event.key === "Enter" && matches[highlight]) {
      event.preventDefault();
      pick(matches[highlight]);
    } else if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
    }
  };

  return (
    <div ref={wrap} className="relative">
      <Input
        value={value}
        onChange={(e) => {
          onChange(e.target.value);
          setOpen(true);
        }}
        onFocus={() => setOpen(true)}
        onKeyDown={onKeyDown}
        placeholder={projects.length ? "Type or pick a project" : "Project name"}
        role="combobox"
        aria-expanded={show}
        aria-controls={listId}
        aria-autocomplete="list"
        autoComplete="off"
      />
      {show ? (
        <ul
          id={listId}
          role="listbox"
          className="absolute z-20 mt-1 max-h-56 w-full overflow-y-auto rounded-xl border border-border bg-surface-raised py-1 shadow-panel"
        >
          {matches.map((name, i) => (
            <li key={name} role="option" aria-selected={i === highlight}>
              <button
                type="button"
                className={clsx(
                  "flex w-full px-3 py-2 text-left text-[13px] transition-colors",
                  i === highlight ? "bg-surface-hover" : "hover:bg-surface-hover",
                )}
                onMouseEnter={() => setHighlight(i)}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => pick(name)}
              >
                {name}
              </button>
            </li>
          ))}
          {value.trim() && !exact ? (
            <li className="border-t border-border px-3 py-2 text-[12px] text-muted">
              Use “{value.trim()}” as a new project
            </li>
          ) : null}
        </ul>
      ) : null}
    </div>
  );
}

/** Mounted only while open, so every opening starts empty. */
export function UploadDialog({ onClose, onUploaded }: { onClose: () => void; onUploaded: () => void }) {
  const { app } = useSession();
  const { data: facets } = useApi<Facets>("/documents/facets");
  const [rows, setRows] = useState<Row[]>([]);
  const [project, setProject] = useState("");
  const [share, setShare] = useState("");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const busy = rows.some((r) => r.state === "sending");
  const polling = rows.some((r) => r.state === "processing");

  const projects = useMemo(() => {
    const fromFacets = facets?.project.map((f) => f.value) ?? [];
    return [...new Set([...fromFacets, ...app.known_projects])].sort((a, b) => a.localeCompare(b));
  }, [facets, app.known_projects]);

  // Follow every uploaded file until it is searchable (or fails): one request for all of them, every 3 seconds. (One request per
  // file every 2 seconds floods the server when hundreds of files are uploaded at once.)
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(async () => {
      const pending = rows.filter((r) => r.state === "processing" && r.itemId);
      if (!pending.length) return;
      type Status = { item_id: string; stage: string; step: string; error: string | null; document_id: string | null };
      const result = await api<{ items: Status[] }>("/uploads/status", { body: { ids: pending.map((r) => r.itemId) } }).catch(() => null);
      if (!result) return;
      const byId = new Map(result.items.map((s) => [s.item_id, s]));
      let finished = false;
      setRows((list) =>
        list.map((row) => {
          const s = row.itemId ? byId.get(row.itemId) : undefined;
          if (!s || row.state !== "processing") return row;
          if (s.stage === "live") {
            finished = true;
            return { ...row, state: "done", documentId: s.document_id, detail: "Searchable now" };
          }
          if (s.stage === "failed") return { ...row, state: "failed", detail: s.error || "Could not be read" };
          return { ...row, detail: s.step === "indexing" ? "Read: checking for sensitive values, then indexing" : "Reading the file" };
        }),
      );
      if (finished) onUploaded();
      // the privacy line (what was found and masked) is fetched for a few finished files per round, not for all at once
      const needPrivacy = pending.filter((r) => !r.privacy && ["indexing", "ready"].includes(byId.get(r.itemId as string)?.step ?? "")).slice(0, 3);
      for (const r of needPrivacy) {
        const one = await api<{ privacy: PrivacyNote | null }>(`/uploads/${r.itemId}`).catch(() => null);
        if (one?.privacy) setRows((list) => list.map((x) => (x.key === r.key ? { ...x, privacy: one.privacy } : x)));
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [polling, rows, onUploaded]);

  const add = (files: FileList | null) => {
    if (!files) return;
    const next = Array.from(files).map((file) => ({ key: `${file.name}-${file.size}-${file.lastModified}`, file, state: "ready" as const }));
    setRows((list) => [...list.filter((r) => !next.some((n) => n.key === r.key)), ...next].slice(0, 50));
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    add(event.dataTransfer.files);
  };

  const upload = async () => {
    const ready = rows.filter((r) => r.state === "ready");
    if (!ready.length) return;
    setError(null);
    setRows((list) => list.map((r) => (r.state === "ready" ? { ...r, state: "sending", detail: "Uploading" } : r)));
    const form = new FormData();
    for (const r of ready) form.append("files", r.file, r.file.name);
    form.append("project", project.trim());
    form.append("share_with", share.trim());
    try {
      const result = await api<{ files: { file: string; ok: boolean; error?: string; item_id?: string }[] }>("/uploads", { form });
      setRows((list) =>
        list.map((r) => {
          if (r.state !== "sending") return r;
          const res = result.files.find((f) => f.file === r.file.name) ?? result.files.find((f) => r.file.name.includes(f.file));
          if (!res) return { ...r, state: "failed", detail: "No result from the server" };
          return res.ok ? { ...r, state: "processing", itemId: res.item_id, detail: "Queued for reading" } : { ...r, state: "failed", detail: res.error };
        }),
      );
    } catch (e) {
      setError(errorMessage(e));
      setRows((list) => list.map((r) => (r.state === "sending" ? { ...r, state: "ready", detail: undefined } : r)));
    }
  };

  const icon = (row: Row) => {
    if (row.state === "done") return <CircleCheck size={15} className="text-success" aria-label="Done" />;
    if (row.state === "failed") return <CircleX size={15} className="text-danger" aria-label="Failed" />;
    if (row.state === "ready") return <FileUp size={15} className="text-muted" aria-hidden />;
    return <LoaderCircle size={15} className="animate-spin text-muted motion-reduce:animate-none" aria-label="Working" />;
  };

  return (
    <Dialog
      open
      onClose={onClose}
      title="Upload documents"
      wide
      footer={
        <>
          <Button onClick={onClose}>{polling ? "Close (keeps processing)" : "Close"}</Button>
          <Button variant="primary" onClick={upload} disabled={busy || !rows.some((r) => r.state === "ready")} icon={<FileUp size={15} aria-hidden />}>
            Upload {rows.filter((r) => r.state === "ready").length || ""}
          </Button>
        </>
      }
    >
      <div
        onDragOver={(e) => {
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
        className={clsx(
          "flex flex-col items-center justify-center rounded-xl border border-dashed px-6 py-8 text-center transition-colors",
          dragging ? "border-foreground bg-surface-hover" : "border-border-strong",
        )}
      >
        <FileUp size={22} strokeWidth={1.6} className="text-muted" aria-hidden />
        <p className="mt-2 text-[13.5px]">Drop files here, or</p>
        <Button size="sm" className="mt-2" onClick={() => input.current?.click()}>
          Choose files
        </Button>
        <p className="mt-3 text-[12px] text-muted">PDF, Word, Excel, PowerPoint, e-mail, images and ZIP. Up to 50 files at a time.</p>
        <input ref={input} type="file" multiple accept={ACCEPT} className="hidden" onChange={(e) => add(e.target.files)} />
      </div>

      {rows.length ? (
        <ul className="divide-y divide-border rounded-xl border border-border">
          {rows.map((row) => (
            <li key={row.key} className="flex items-center gap-3 px-3 py-2 text-[13px]">
              {icon(row)}
              <span className="min-w-0 flex-1">
                <span className="block truncate">{row.file.name}</span>
                <span className={clsx("block truncate text-[12px]", row.state === "failed" ? "text-danger" : "text-muted")}>{row.detail ?? size(row.file.size)}</span>
                {row.privacy ? <PrivacyLine note={row.privacy} /> : null}
              </span>
              {row.state === "done" && row.documentId ? (
                <Link href={`/documents/${row.documentId}`} className="text-[12.5px] underline underline-offset-2">
                  Open
                </Link>
              ) : null}
              {row.state === "ready" ? (
                <button type="button" onClick={() => setRows((list) => list.filter((r) => r.key !== row.key))} className="rounded p-1 text-muted hover:text-foreground" aria-label={`Remove ${row.file.name}`}>
                  <X size={14} />
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        <Field label="Project" hint="Optional. Pick an existing project or type a new name.">
          <ProjectPicker value={project} onChange={setProject} projects={projects} />
        </Field>
        <Field label="Share with" hint="Only you see your uploads, unless you add groups you belong to.">
          <Input value={share} onChange={(e) => setShare(e.target.value)} placeholder="lgroup:commercial" />
        </Field>
      </div>
      {error ? <p className="text-[13px] text-danger">{error}</p> : null}
    </Dialog>
  );
}
