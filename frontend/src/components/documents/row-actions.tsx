"use client";

import { Cloud, Cpu, Pencil, RefreshCw, Trash2 } from "lucide-react";
import { useState } from "react";

import { ProjectPicker } from "@/components/documents/upload-dialog";
import { Button, IconButton } from "@/components/ui/button";
import { Field } from "@/components/ui/fields";
import { Dialog } from "@/components/ui/overlay";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { useApi } from "@/lib/hooks";

/** The actions at the end of a row, as icons: edit the project, read the file again, delete. */
export function RowActions({ title, onEdit, onReprocess, onDelete }: { title: string; onEdit: () => void; onReprocess: () => void; onDelete: () => void }) {
  return (
    <div className="flex items-center justify-end gap-0.5">
      <IconButton label={`Edit project of ${title}`} onClick={onEdit}>
        <Pencil size={15} />
      </IconButton>
      <IconButton label={`Reprocess ${title}`} onClick={onReprocess}>
        <RefreshCw size={15} />
      </IconButton>
      <IconButton label={`Delete ${title}`} onClick={onDelete} className="hover:!text-danger">
        <Trash2 size={15} />
      </IconButton>
    </div>
  );
}

/** Set, change or clear the project of one document. */
export function EditProjectDialog({
  id, title, current, projects, onClose, onSaved,
}: { id: string; title: string; current: string; projects: string[]; onClose: () => void; onSaved: () => void }) {
  const [value, setValue] = useState(current);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const save = async () => {
    setBusy(true);
    try {
      await api(`/documents/${id}`, { method: "PATCH", body: { project: value.trim() } });
      toast(value.trim() ? `Project set to ${value.trim()}` : "Project cleared");
      onSaved();
      onClose();
    } catch (e) {
      toast(errorMessage(e), "error");
      setBusy(false);
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title="Edit project"
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} disabled={busy}>Save</Button>
        </>
      }
    >
      <p className="truncate text-[13px] text-muted">{title}</p>
      <Field label="Project" hint="Pick an existing project or type a new name. Leave it empty to remove the project.">
        <ProjectPicker value={value} onChange={setValue} projects={projects} />
      </Field>
    </Dialog>
  );
}

/** Ask before deleting one or many documents. */
export function DeleteDialog({
  ids, label, onClose, onDeleted,
}: { ids: string[]; label: string; onClose: () => void; onDeleted: () => void }) {
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const remove = async () => {
    setBusy(true);
    try {
      const result = await api<{ removed: number }>("/documents/bulk-delete", { body: { ids } });
      toast(`Deleted ${result.removed} document${result.removed === 1 ? "" : "s"}`);
      onDeleted();
      onClose();
    } catch (e) {
      toast(errorMessage(e), "error");
      setBusy(false);
    }
  };
  return (
    <Dialog
      open
      onClose={onClose}
      title={ids.length === 1 ? "Delete this document?" : `Delete ${ids.length} documents?`}
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="danger" onClick={remove} disabled={busy}>{busy ? "Deleting…" : "Delete"}</Button>
        </>
      }
    >
      <p className="text-[13.5px]">{label}</p>
      <p className="text-[13px] text-muted">
        The original file, its text, chunks and search entries are removed for good. This cannot be undone.
      </p>
    </Dialog>
  );
}

type Options = { local: { label: string }; vision: { available: boolean; model: string | null }; current: "local" | "vision" };

/** Read a file again, choosing who does the OCR for this run only. */
export function ReprocessDialog({ id, title, onClose, onQueued }: { id: string; title: string; onClose: () => void; onQueued: () => void }) {
  const { data } = useApi<Options>("/documents/reprocess-options");
  const [choice, setChoice] = useState<"local" | "vision" | null>(null);
  const [busy, setBusy] = useState(false);
  const toast = useToast();
  const mode = choice ?? data?.current ?? "local";
  const visionOk = Boolean(data?.vision.available);
  const run = async () => {
    setBusy(true);
    try {
      await api(`/documents/${id}/reprocess`, { body: { mode } });
      toast("Reading the file again. Progress shows at the top of the page.");
      onQueued();
      onClose();
    } catch (e) {
      toast(errorMessage(e), "error");
      setBusy(false);
    }
  };
  const option = (value: "local" | "vision", icon: React.ReactNode, name: string, text: string, disabled = false) => (
    <label
      className={`flex cursor-pointer items-start gap-3 rounded-xl border px-3.5 py-3 ${mode === value ? "border-foreground bg-surface-hover" : "border-border"} ${disabled ? "cursor-not-allowed opacity-50" : "hover:bg-surface-hover"}`}
    >
      <input type="radio" name="ocr" checked={mode === value} disabled={disabled} onChange={() => setChoice(value)} className="mt-1 accent-[var(--accent)]" />
      <span className="min-w-0">
        <span className="flex items-center gap-1.5 text-[13.5px] font-medium">
          {icon} {name}
        </span>
        <span className="block text-[12.5px] text-muted">{text}</span>
      </span>
    </label>
  );
  return (
    <Dialog
      open
      onClose={onClose}
      title="Reprocess this file"
      footer={
        <>
          <Button onClick={onClose} disabled={busy}>Cancel</Button>
          <Button variant="primary" onClick={run} disabled={busy || !data} icon={<RefreshCw size={14} aria-hidden />}>
            {busy ? "Queuing…" : "Reprocess"}
          </Button>
        </>
      }
    >
      <p className="truncate text-[13px] text-muted">{title}</p>
      <p className="text-[13px]">The file is read again from the stored original, then split, embedded and published. It stays searchable meanwhile. Choose how scans and images are read this time:</p>
      <div className="space-y-2">
        {option("local", <Cpu size={14} aria-hidden />, "Local OCR", `${data?.local.label ?? "RapidOCR"}: everything stays on this server and costs nothing. Best for clean scans; weaker on photos, stamps and Arabic handwriting.`)}
        {option(
          "vision",
          <Cloud size={14} aria-hidden />,
          "LLM OCR (vision model)",
          visionOk
            ? `${data?.vision.model}: reads scans, photos and tables more accurately, and also describes images. Pages are sent to the model (those with bank or ID numbers stay local) and cost a little per page.`
            : "No vision model is set. Choose one under Models, Drawing and image reading, to use this.",
          !visionOk,
        )}
      </div>
      {data ? <p className="text-[12px] text-muted">Your current default is {data.current === "vision" ? "LLM OCR" : "local OCR"}. This choice applies to this file only.</p> : null}
    </Dialog>
  );
}
