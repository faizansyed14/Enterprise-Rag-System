"use client";

import clsx from "clsx";
import { ChevronRight, CircleCheck, CircleX, FileArchive, FileUp, Folder, LoaderCircle, Lock } from "lucide-react";
import Link from "next/link";
import { Fragment, useEffect, useMemo, useRef, useState, type DragEvent, type KeyboardEvent, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/fields";
import { Dialog } from "@/components/ui/overlay";
import { PrivacyLine, type PrivacyNote } from "@/components/documents/privacy";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import { useSession } from "@/lib/session";
import type { Facets } from "@/lib/types";

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

/* ---- what is in the dialog ------------------------------------------------------------------------------------------- */

type Options = { deselected_by_default: string[]; supported: string[]; max_file_mb: number; max_zip_mb: number };

type Item = {
  key: string;
  /** Where it came from: just the name for a file chosen on its own, "Site.zip/Drawings/a.pdf" for a file inside a ZIP. */
  path: string;
  name: string;
  ext: string;
  size: number;
  selected: boolean;
  /** False when no reader exists for the type (the file is listed, but cannot be ticked). */
  supported: boolean;
  problem?: string | null;
  /** Why the file cannot be read (password or rights protection), found before upload; it cannot be ticked. */
  protected?: string | null;
  file?: File;
  zipKey?: string;
  stagingId?: string;
  memberId?: string;
  state: "ready" | "sending" | "processing" | "done" | "failed";
  detail?: string;
  itemId?: string;
  documentId?: string | null;
  privacy?: PrivacyNote | null;
};

type Zip = { key: string; name: string; size: number; state: "opening" | "ready" | "error"; stagingId?: string; error?: string; hours?: number };

type ExtractedFile = { id: string; path: string; name: string; ext: string; size: number; selected: boolean; supported: boolean; problem: string | null; protected: string | null };

type Result = { id?: string; file: string; ok: boolean; error?: string; item_id?: string; protected?: boolean };

const PROTECTED = "Protected: ";
const OFFICE_LOCKED = `${PROTECTED}this Office file is encrypted (password or rights management); it cannot be read without it.`;
const OLE_HEADER = [0xd0, 0xcf, 0x11, 0xe0];

const extOf = (name: string) => {
  const dot = name.lastIndexOf(".");
  return dot > 0 && dot < name.length - 1 ? name.slice(dot + 1).toLowerCase() : "";
};

function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

const canTick = (item: Item) => item.supported && !item.problem && !item.protected && item.state === "ready";

type TreeNode = { name: string; path: string; folders: Map<string, TreeNode>; files: Item[] };
type Count = { total: number; selected: number; selectable: number };

function buildTree(items: Item[]): TreeNode {
  const root: TreeNode = { name: "", path: "", folders: new Map(), files: [] };
  for (const item of items) {
    const parts = item.path.split("/");
    let node = root;
    for (let i = 0; i < parts.length - 1; i++) {
      const path = parts.slice(0, i + 1).join("/");
      let next = node.folders.get(parts[i]);
      if (!next) {
        next = { name: parts[i], path, folders: new Map(), files: [] };
        node.folders.set(parts[i], next);
      }
      node = next;
    }
    node.files.push(item);
  }
  return root;
}

function countOf(node: TreeNode, memo: Map<string, Count>): Count {
  const own = node.files.reduce<Count>(
    (c, f) => ({ total: c.total + 1, selected: c.selected + (f.selected ? 1 : 0), selectable: c.selectable + (canTick(f) ? 1 : 0) }),
    { total: 0, selected: 0, selectable: 0 },
  );
  for (const child of node.folders.values()) {
    const c = countOf(child, memo);
    own.total += c.total;
    own.selected += c.selected;
    own.selectable += c.selectable;
  }
  memo.set(node.path, own);
  return own;
}

function filesUnder(node: TreeNode, out: Item[] = []): Item[] {
  out.push(...node.files);
  for (const child of node.folders.values()) filesUnder(child, out);
  return out;
}

function TickBox({ checked, indeterminate, disabled, onChange, label }: { checked: boolean; indeterminate?: boolean; disabled?: boolean; onChange: () => void; label: string }) {
  const ref = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = Boolean(indeterminate) && !checked;
  }, [indeterminate, checked]);
  return <input ref={ref} type="checkbox" checked={checked} disabled={disabled} onChange={onChange} aria-label={label} className="h-3.5 w-3.5 shrink-0 accent-[var(--accent)] disabled:opacity-40" />;
}

const PER_FOLDER = 300; // files drawn per folder before "Show more" (a folder can hold thousands)
const OPEN_ALL_BELOW = 60; // lists up to this long start fully open; longer ones show the top folders only

/** Mounted only while open, so every opening starts empty. */
export function UploadDialog({ onClose, onUploaded }: { onClose: () => void; onUploaded: () => void }) {
  const { app } = useSession();
  const { data: facets } = useApi<Facets>("/documents/facets");
  const { data: options } = useApi<Options>("/uploads/options");
  const [items, setItems] = useState<Item[]>([]);
  const [zips, setZips] = useState<Zip[]>([]);
  const [project, setProject] = useState("");
  const [share, setShare] = useState("");
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [more, setMore] = useState<Record<string, number>>({});
  const input = useRef<HTMLInputElement>(null);
  const zipsRef = useRef<Zip[]>([]);
  useEffect(() => {
    zipsRef.current = zips;
  }, [zips]);

  const busy = items.some((r) => r.state === "sending") || zips.some((z) => z.state === "opening");
  const polling = items.some((r) => r.state === "processing");
  const defaultOff = useMemo(() => new Set(options?.deselected_by_default ?? ["jpg", "jpeg", "png"]), [options]);
  const readable = useMemo(() => new Set(options?.supported ?? []), [options]);

  const projects = useMemo(() => {
    const fromFacets = facets?.project.map((f) => f.value) ?? [];
    return [...new Set([...fromFacets, ...app.known_projects])].sort((a, b) => a.localeCompare(b));
  }, [facets, app.known_projects]);

  // Closing the dialog removes the opened ZIPs from the server (it also drops any that nobody came back to).
  const close = () => {
    for (const z of zipsRef.current) if (z.stagingId) api(`/uploads/staged/${z.stagingId}`, { method: "DELETE" }).catch(() => undefined);
    onClose();
  };

  // Follow every uploaded file until it is searchable (or fails): one request per 1000 files, every 3 seconds.
  useEffect(() => {
    if (!polling) return;
    const timer = window.setInterval(async () => {
      const pending = items.filter((r) => r.state === "processing" && r.itemId);
      if (!pending.length) return;
      type Status = { item_id: string; stage: string; step: string; error: string | null; document_id: string | null };
      const found: Status[] = [];
      for (let i = 0; i < pending.length; i += 1000) {
        const result = await api<{ items: Status[] }>("/uploads/status", { body: { ids: pending.slice(i, i + 1000).map((r) => r.itemId) } }).catch(() => null);
        if (result) found.push(...result.items);
      }
      if (!found.length) return;
      const byId = new Map(found.map((s) => [s.item_id, s]));
      let finished = false;
      setItems((list) =>
        list.map((row) => {
          const s = row.itemId ? byId.get(row.itemId) : undefined;
          if (!s || row.state !== "processing") return row;
          if (s.stage === "live") {
            finished = true;
            return { ...row, state: "done", documentId: s.document_id, detail: "Searchable / live" };
          }
          if (s.stage === "failed") {
            const locked = s.error?.startsWith(PROTECTED) ? s.error : undefined;
            return { ...row, state: "failed", protected: locked ?? row.protected, detail: locked ? undefined : s.error || "Could not be read" };
          }
          const stepDetail =
            s.step === "queued"
              ? "Waiting to be picked up"
              : s.step === "indexing"
                ? "Chunking, embedding, and publishing"
                : s.step === "reading"
                  ? "Extracting text (OCR or vision when needed)"
                  : s.step || "Processing";
          return { ...row, detail: stepDetail };
        }),
      );
      if (finished) onUploaded();
      // the privacy line (what was found and masked) is fetched for a few finished files per round, not for all at once
      const needPrivacy = pending.filter((r) => !r.privacy && ["indexing", "ready"].includes(byId.get(r.itemId as string)?.step ?? "")).slice(0, 3);
      for (const r of needPrivacy) {
        const one = await api<{ privacy: PrivacyNote | null }>(`/uploads/${r.itemId}`).catch(() => null);
        if (one?.privacy) setItems((list) => list.map((x) => (x.key === r.key ? { ...x, privacy: one.privacy } : x)));
      }
    }, 3000);
    return () => window.clearInterval(timer);
  }, [polling, items, onUploaded]);

  /* ---- adding files: single files are listed at once, a ZIP is opened first ---------------------------------------- */
  const openZip = async (file: File) => {
    const key = `zip-${file.name}-${file.size}-${file.lastModified}`;
    if (zipsRef.current.some((z) => z.key === key)) return;
    setZips((list) => [...list, { key, name: file.name, size: file.size, state: "opening" }]);
    try {
      const form = new FormData();
      form.append("file", file, file.name);
      const result = await api<{ staging_id: string; name: string; files: ExtractedFile[]; expires_in_hours: number }>("/uploads/extract", { form });
      setZips((list) => list.map((z) => (z.key === key ? { ...z, state: "ready", stagingId: result.staging_id, hours: result.expires_in_hours } : z)));
      setItems((list) => [
        ...list,
        ...result.files.map<Item>((f) => ({
          key: `${key}/${f.id}`,
          path: `${file.name}/${f.path}`,
          name: f.name,
          ext: f.ext,
          size: f.size,
          selected: f.selected,
          supported: f.supported,
          problem: f.protected ? null : f.problem,
          protected: f.protected,
          zipKey: key,
          stagingId: result.staging_id,
          memberId: f.id,
          state: "ready",
        })),
      ]);
    } catch (e) {
      setZips((list) => list.map((z) => (z.key === key ? { ...z, state: "error", error: errorMessage(e) } : z)));
    }
  };

  const add = async (files: FileList | null) => {
    if (!files) return;
    const chosen = Array.from(files);
    const singles: Item[] = [];
    for (const file of chosen) {
      if (extOf(file.name) === "zip") {
        void openZip(file);
        continue;
      }
      const ext = extOf(file.name);
      const supported = !options || !ext || readable.has(ext);
      // an encrypted .docx/.xlsx/.pptx is not a ZIP any more: its first bytes give it away (other kinds are checked by the server on upload)
      let locked: string | null = null;
      if (/^(docx|docm|xlsx|xlsm|pptx|pptm)$/.test(ext)) {
        const head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
        if (OLE_HEADER.every((byte, i) => head[i] === byte)) locked = OFFICE_LOCKED;
      }
      singles.push({
        key: `${file.name}-${file.size}-${file.lastModified}`,
        path: file.name,
        name: file.name,
        ext,
        size: file.size,
        selected: supported && !locked && !defaultOff.has(ext),
        supported,
        problem: supported ? null : `.${ext} files cannot be read yet`,
        protected: locked,
        file,
        state: "ready",
      });
    }
    setItems((list) => [...list.filter((r) => !singles.some((n) => n.key === r.key)), ...singles]);
  };
  const onDrop = (event: DragEvent) => {
    event.preventDefault();
    setDragging(false);
    void add(event.dataTransfer.files);
  };

  /* ---- ticking ----------------------------------------------------------------------------------------------------- */
  const setTicks = (match: (item: Item) => boolean, value: boolean) =>
    setItems((list) => list.map((r) => (canTick(r) && match(r) ? { ...r, selected: value } : r)));
  const toggleOne = (key: string) => setItems((list) => list.map((r) => (r.key === key && canTick(r) ? { ...r, selected: !r.selected } : r)));

  const total = items.length;
  const selected = items.filter((r) => r.selected && r.state === "ready").length;
  const lockedCount = items.filter((r) => r.protected).length;
  const selectedBytes = items.filter((r) => r.selected && r.state === "ready").reduce((n, r) => n + r.size, 0);
  const formats = useMemo(() => {
    const map = new Map<string, { total: number; selected: number; selectable: number }>();
    for (const r of items) {
      const f = map.get(r.ext) ?? { total: 0, selected: 0, selectable: 0 };
      f.total += 1;
      f.selected += r.selected ? 1 : 0;
      f.selectable += canTick(r) ? 1 : 0;
      map.set(r.ext, f);
    }
    return [...map.entries()].sort((a, b) => b[1].total - a[1].total || a[0].localeCompare(b[0]));
  }, [items]);
  const tree = useMemo(() => buildTree(items), [items]);
  const counts = useMemo(() => {
    const memo = new Map<string, Count>();
    countOf(tree, memo);
    return memo;
  }, [tree]);

  /* ---- uploading what is ticked ------------------------------------------------------------------------------------- */
  const upload = async () => {
    const ready = items.filter((r) => r.selected && r.state === "ready" && canTick(r));
    if (!ready.length) return;
    setError(null);
    const keys = new Set(ready.map((r) => r.key));
    setItems((list) => list.map((r) => (keys.has(r.key) ? { ...r, state: "sending", detail: "Uploading" } : r)));
    const apply = (key: string, res: Result | undefined) =>
      setItems((list) =>
        list.map((r) => {
          if (r.key !== key) return r;
          if (!res) return { ...r, state: "failed", detail: "No result from the server" };
          if (res.ok) return { ...r, state: "processing", itemId: res.item_id, detail: "Queued for reading" };
          return res.protected ? { ...r, state: "failed", protected: res.error, detail: undefined } : { ...r, state: "failed", detail: res.error };
        }),
      );
    const fail = (list: Item[], e: unknown) => {
      const gone = new Set(list.map((r) => r.key));
      setError(errorMessage(e));
      setItems((all) => all.map((r) => (gone.has(r.key) ? { ...r, state: "ready", detail: undefined } : r)));
    };
    // files chosen one by one: 50 per request
    const singles = ready.filter((r) => r.file);
    for (let i = 0; i < singles.length; i += 50) {
      const part = singles.slice(i, i + 50);
      const form = new FormData();
      for (const r of part) form.append("files", r.file as File, r.file?.name);
      form.append("project", project.trim());
      form.append("share_with", share.trim());
      try {
        const result = await api<{ files: Result[] }>("/uploads", { form });
        part.forEach((r, n) => apply(r.key, result.files[n]));
      } catch (e) {
        fail(part, e);
      }
    }
    // files inside opened ZIPs: one request per ZIP, naming the ticked files
    for (const stagingId of new Set(ready.filter((r) => r.stagingId).map((r) => r.stagingId as string))) {
      const part = ready.filter((r) => r.stagingId === stagingId);
      try {
        const result = await api<{ files: Result[] }>("/uploads/commit", {
          body: { staging_id: stagingId, ids: part.map((r) => r.memberId), project: project.trim(), share_with: share.trim() },
        });
        const byId = new Map(result.files.map((f) => [f.id, f]));
        part.forEach((r) => apply(r.key, byId.get(r.memberId as string)));
      } catch (e) {
        fail(part, e);
      }
    }
  };

  const icon = (row: Item) => {
    if (row.state === "done") return <CircleCheck size={15} className="text-success" aria-label="Done" />;
    if (row.state === "failed") return <CircleX size={15} className="text-danger" aria-label="Failed" />;
    if (row.state === "ready") return null;
    return <LoaderCircle size={15} className="animate-spin text-muted motion-reduce:animate-none" aria-label="Processing" />;
  };

  const fileRow = (row: Item, depth: number) => (
    <li className="flex items-center gap-2.5 py-1.5 pr-3 text-[13px]" style={{ paddingLeft: 12 + depth * 18 }}>
      <TickBox checked={row.selected} disabled={!canTick(row)} onChange={() => toggleOne(row.key)} label={`${row.selected ? "Deselect" : "Select"} ${row.name}`} />
      {icon(row)}
      <span className="min-w-0 flex-1">
        <span className={clsx("block truncate", !row.selected && row.state === "ready" && "text-muted")} title={row.path}>
          {row.name}
        </span>
        {row.protected ? (
          <span className="mt-0.5 flex items-start gap-1 text-[12px] text-warning">
            <Lock size={12} className="mt-[3px] shrink-0" aria-hidden />
            <span className="font-medium">Protected</span>
            <span className="min-w-0">{row.protected.replace(PROTECTED, "")}</span>
          </span>
        ) : row.problem || row.detail ? (
          <span className={clsx("block truncate text-[12px]", row.state === "failed" || row.problem ? "text-warning" : "text-muted")}>{row.problem ?? row.detail}</span>
        ) : null}
        {row.privacy ? <PrivacyLine note={row.privacy} /> : null}
      </span>
      <span className="shrink-0 text-[11.5px] uppercase text-muted">{row.ext || "—"}</span>
      <span className="w-16 shrink-0 text-right text-[12px] tabular text-muted">{size(row.size)}</span>
      {row.state === "done" && row.documentId ? (
        <Link href={`/documents/${row.documentId}`} className="text-[12.5px] underline underline-offset-2">
          Open
        </Link>
      ) : null}
    </li>
  );

  const renderNode = (node: TreeNode, depth: number): ReactNode => {
    const folders = [...node.folders.values()].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    const files = [...node.files].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }));
    const limit = more[node.path] ?? PER_FOLDER;
    return (
      <>
        {folders.map((folder) => {
          const c = counts.get(folder.path) ?? { total: 0, selected: 0, selectable: 0 };
          const expanded = open[folder.path] ?? (depth < 2 || total <= OPEN_ALL_BELOW);
          const all = c.selectable > 0 && filesUnder(folder).filter(canTick).every((f) => f.selected);
          return (
            <li key={folder.path}>
              <div className="flex items-center gap-2 bg-surface/60 py-1.5 pr-3 text-[13px]" style={{ paddingLeft: 12 + depth * 18 }}>
                <TickBox
                  checked={all}
                  indeterminate={c.selected > 0 && !all}
                  disabled={c.selectable === 0}
                  onChange={() => {
                    const members = new Set(filesUnder(folder).map((f) => f.key));
                    setTicks((r) => members.has(r.key), !all);
                  }}
                  label={`${all ? "Deselect" : "Select"} everything in ${folder.name}`}
                />
                <button type="button" onClick={() => setOpen((o) => ({ ...o, [folder.path]: !expanded }))} className="flex min-w-0 flex-1 items-center gap-1.5 text-left" aria-expanded={expanded}>
                  <ChevronRight size={13} className={clsx("shrink-0 text-muted transition-transform", expanded && "rotate-90")} aria-hidden />
                  {folder.name.toLowerCase().endsWith(".zip") ? <FileArchive size={14} className="shrink-0 text-muted" aria-hidden /> : <Folder size={14} className="shrink-0 text-muted" aria-hidden />}
                  <span className="truncate font-medium">{folder.name}</span>
                </button>
                <span className="shrink-0 text-[12px] tabular text-muted">
                  {c.selected} of {c.total} selected
                </span>
              </div>
              {expanded ? <ul>{renderNode(folder, depth + 1)}</ul> : null}
            </li>
          );
        })}
        {files.slice(0, limit).map((row) => (
          <Fragment key={row.key}>{fileRow(row, depth)}</Fragment>
        ))}
        {files.length > limit ? (
          <li className="py-1.5 text-[12.5px]" style={{ paddingLeft: 12 + depth * 18 }}>
            <button type="button" className="text-muted underline underline-offset-2 hover:text-foreground" onClick={() => setMore((m) => ({ ...m, [node.path]: limit + PER_FOLDER }))}>
              Show {Math.min(PER_FOLDER, files.length - limit)} more of {files.length - limit}
            </button>
          </li>
        ) : null}
      </>
    );
  };

  const nothingYet = !items.length && !zips.length;

  return (
    <Dialog
      open
      onClose={close}
      title="Upload documents"
      wide
      footer={
        <>
          <Button onClick={close}>{polling ? "Close (keeps processing)" : "Close"}</Button>
          <Button variant="primary" onClick={upload} disabled={busy || selected === 0} icon={<FileUp size={15} aria-hidden />}>
            Upload {selected || ""}
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
          "flex flex-col items-center justify-center rounded-xl border border-dashed px-6 text-center transition-colors",
          nothingYet ? "py-8" : "py-4",
          dragging ? "border-foreground bg-surface-hover" : "border-border-strong",
        )}
      >
        <FileUp size={22} strokeWidth={1.6} className="text-muted" aria-hidden />
        <p className="mt-2 text-[13.5px]">Drop files or a ZIP folder here, or</p>
        <Button size="sm" className="mt-2" onClick={() => input.current?.click()}>
          Choose files
        </Button>
        {nothingYet ? (
          <p className="mt-3 max-w-[560px] text-[12px] text-muted">
            A ZIP is opened first, with every folder and ZIP inside it, and you choose which files to upload. Documents and AutoCAD drawings (DWG, DXF)
            are ticked; images (JPG, PNG) and other files are listed unticked, and you can tick them. ZIP up to {options ? Math.round(options.max_zip_mb / 1024) : 4} GB, each file up to {options?.max_file_mb ?? 200} MB.
          </p>
        ) : null}
        <input
          ref={input}
          type="file"
          multiple
          className="hidden"
          onChange={(e) => {
            void add(e.target.files);
            e.target.value = "";
          }}
        />
      </div>

      {zips.length ? (
        <ul className="space-y-1">
          {zips.map((z) => (
            <li key={z.key} className="flex items-center gap-2 text-[12.5px]">
              {z.state === "opening" ? <LoaderCircle size={14} className="animate-spin text-muted" aria-hidden /> : z.state === "error" ? <CircleX size={14} className="text-danger" aria-hidden /> : <FileArchive size={14} className="text-muted" aria-hidden />}
              <span className="truncate font-medium">{z.name}</span>
              <span className="text-muted">
                {z.state === "opening" ? `${size(z.size)}: uploading and opening every folder and ZIP inside…` : z.state === "error" ? z.error : "opened"}
              </span>
            </li>
          ))}
        </ul>
      ) : null}

      {items.length ? (
        <>
          <div className="flex flex-wrap items-center gap-x-4 gap-y-2 rounded-xl border border-border px-3 py-2 text-[13px]">
            <span className="tabular">
              <b>{total.toLocaleString()}</b> files found
            </span>
            <span className="tabular text-success">
              <b>{selected.toLocaleString()}</b> selected ({size(selectedBytes)})
            </span>
            <span className="tabular text-muted">
              <b>{(total - selected).toLocaleString()}</b> not selected
            </span>
            {lockedCount ? (
              <span className="inline-flex items-center gap-1 tabular text-warning" title="Password- or rights-protected files cannot be read, so they cannot be ticked">
                <Lock size={13} aria-hidden />
                <b>{lockedCount.toLocaleString()}</b> protected
              </span>
            ) : null}
            <span className="ml-auto flex gap-1">
              <Button size="sm" onClick={() => setTicks(() => true, true)}>
                Select all
              </Button>
              <Button size="sm" onClick={() => setTicks(() => true, false)}>
                Deselect all
              </Button>
            </span>
          </div>
          <div className="flex flex-wrap gap-1.5" aria-label="Files by format">
            {formats.map(([ext, f]) => {
              const every = f.selectable > 0 && f.selected >= f.selectable;
              return (
                <button
                  key={ext || "none"}
                  type="button"
                  disabled={f.selectable === 0}
                  onClick={() => setTicks((r) => r.ext === ext, !every)}
                  title={f.selectable === 0 ? "No reader exists for this type yet" : every ? `Deselect all ${ext.toUpperCase() || "files without an extension"}` : `Select all ${ext.toUpperCase() || "files without an extension"}`}
                  className={clsx(
                    "rounded-full border px-2.5 py-0.5 text-[12px] tabular transition-colors disabled:cursor-not-allowed disabled:opacity-60",
                    f.selected > 0 ? "border-foreground/40 bg-surface-hover text-foreground" : "border-border text-muted hover:text-foreground",
                  )}
                >
                  {ext ? ext.toUpperCase() : "no extension"} · {f.selected}/{f.total}
                </button>
              );
            })}
          </div>
          <ul className="max-h-[44vh] divide-y divide-border/60 overflow-y-auto rounded-xl border border-border">{renderNode(tree, 0)}</ul>
        </>
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
