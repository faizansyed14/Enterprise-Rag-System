"use client";

import { MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import { useState } from "react";

import { ProjectPicker } from "@/components/documents/upload-dialog";
import { Button, IconButton } from "@/components/ui/button";
import { Field } from "@/components/ui/fields";
import { Dialog, Popover } from "@/components/ui/overlay";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";

/** The three dots at the end of a row: change the project, or delete the file. */
export function RowMenu({ title, onEdit, onDelete }: { title: string; onEdit: () => void; onDelete: () => void }) {
  const [open, setOpen] = useState(false);
  const item = "flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-[13px] hover:bg-surface-hover";
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      align="right"
      className="w-44 p-1"
      trigger={(props) => (
        <IconButton label={`Actions for ${title}`} {...props}>
          <MoreHorizontal size={16} />
        </IconButton>
      )}
    >
      <button type="button" className={item} onClick={() => { setOpen(false); onEdit(); }}>
        <Pencil size={14} aria-hidden /> Edit project
      </button>
      <button type="button" className={`${item} text-danger`} onClick={() => { setOpen(false); onDelete(); }}>
        <Trash2 size={14} aria-hidden /> Delete
      </button>
    </Popover>
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
