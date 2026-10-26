"use client";

import { ArrowLeft, ChevronLeft, ChevronRight, FileUp, Folder, FolderOpen, Search, Trash2 } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ProcessingQueue } from "@/components/documents/processing-queue";
import { DeleteDialog, EditProjectDialog, ReprocessDialog, RowActions } from "@/components/documents/row-actions";
import { UploadDialog } from "@/components/documents/upload-dialog";
import { Button, IconButton } from "@/components/ui/button";
import { EmptyState, ErrorNote, Spinner } from "@/components/ui/feedback";
import { Input, Select } from "@/components/ui/fields";
import { PageHeader, Table, Td, Th } from "@/components/ui/layout";
import { docTypeLabel, shortDate, titleCase } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import type { DocumentRow, Facets } from "@/lib/types";

const PAGE = 50;
const NO_PROJECT = "__none__"; // the folder of files that have no project yet

function DocumentsPage() {
  const router = useRouter();
  const route = useSearchParams();
  const project = route.get("project") ?? "";
  const showAll = route.get("all") === "1";
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  const [docType, setDocType] = useState("");
  const [sort, setSort] = useState("newest");
  const [offset, setOffset] = useState(0);
  const [uploading, setUploading] = useState(false);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [editing, setEditing] = useState<DocumentRow | null>(null);
  const [reprocessing, setReprocessing] = useState<DocumentRow | null>(null);
  const [deleting, setDeleting] = useState<{ ids: string[]; label: string } | null>(null);

  useEffect(() => {
    document.title = "Documents";
  }, []);
  useEffect(() => {
    const timer = window.setTimeout(() => {
      setDebounced(query.trim());
      setOffset(0);
    }, 250);
    return () => window.clearTimeout(timer);
  }, [query]);
  const pick = (set: (v: string) => void) => (event: { target: { value: string } }) => {
    set(event.target.value);
    setOffset(0);
  };
  const openLibrary = (next: { project?: string; all?: boolean }) => {
    const q = new URLSearchParams(route.toString());
    if (next.project) q.set("project", next.project);
    else q.delete("project");
    if (next.all) q.set("all", "1");
    else q.delete("all");
    const search = q.toString();
    router.replace(search ? `/documents?${search}` : "/documents", { scroll: false });
    setOffset(0);
  };
  const openDocument = (id: string) => {
    const from = project || (showAll ? "__all__" : "");
    router.push(from ? `/documents/${id}?from=${encodeURIComponent(from)}` : `/documents/${id}`);
  };

  const params = new URLSearchParams({ limit: String(PAGE), offset: String(offset) });
  if (debounced) params.set("q", debounced);
  if (project) params.append("project", project);
  if (docType) params.append("doc_type", docType);
  if (sort !== "newest") params.set("sort", sort);
  const { data, error, loading, reload } = useApi<{ total: number; documents: DocumentRow[] }>(`/documents?${params}`);
  const { data: facets, reload: reloadFacets } = useApi<Facets>("/documents/facets");
  const onUploaded = useCallback(() => {
    void reload();
    void reloadFacets();
  }, [reload, reloadFacets]);

  const total = data?.total ?? 0;
  const rows = useMemo(() => data?.documents ?? [], [data]);
  const allSelected = rows.length > 0 && rows.every((d) => selected.has(d.id));
  const someSelected = rows.some((d) => selected.has(d.id));
  const master = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (master.current) master.current.indeterminate = someSelected && !allSelected;
  }, [someSelected, allSelected]);
  // a selection never outlives the list it was made on
  useEffect(() => {
    setSelected((s) => (s.size ? new Set([...s].filter((id) => rows.some((d) => d.id === id))) : s));
  }, [rows]);
  const toggle = (id: string) =>
    setSelected((s) => {
      const next = new Set(s);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  const selectAll = () => setSelected(new Set(rows.map((d) => d.id)));
  const clearSelection = () => setSelected(new Set());
  const afterChange = () => {
    clearSelection();
    onUploaded();
  };
  const filtered = Boolean(debounced || project || docType);
  // with nothing chosen yet, the library opens as folders (one per project); a search or a filter shows files
  const asFolders = !filtered && !showAll;
  const folderName = (value: string) => (value === NO_PROJECT ? "No project" : value);

  return (
    <div>
      <PageHeader
        title="Documents"
        description="Everything you are allowed to open, newest first. Search by document number, title or file name."
        actions={
          <Button variant="primary" icon={<FileUp size={15} aria-hidden />} onClick={() => setUploading(true)}>
            Upload
          </Button>
        }
      />
      <ProcessingQueue onFinished={onUploaded} />
      <div className="flex flex-wrap items-center gap-2 px-6 py-4 lg:px-10">
        <div className="relative min-w-[220px] flex-1">
          <Search size={15} className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-muted" aria-hidden />
          <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="A-101, RFI-023, Marina Tower specification…" className="pl-9" aria-label="Search documents" />
        </div>
        <Select value={project} onChange={(e) => openLibrary({ project: e.target.value, all: false })} className="!w-auto min-w-[160px] max-w-[220px]" aria-label="Project">
          <option value="">All projects</option>
          {facets?.unassigned ? (
            <option value={NO_PROJECT}>
              No project ({facets.unassigned})
            </option>
          ) : null}
          {facets?.project.map((f) => (
            <option key={f.value} value={f.value}>
              {f.value} ({f.count})
            </option>
          ))}
        </Select>
        <Select value={sort} onChange={pick(setSort)} className="!w-auto min-w-[150px] max-w-[200px]" aria-label="Sort by">
          <option value="newest">Recent first</option>
          <option value="oldest">Oldest first</option>
          <option value="name_asc">Name A–Z</option>
          <option value="name_desc">Name Z–A</option>
        </Select>
        <Select value={docType} onChange={pick(setDocType)} className="!w-auto min-w-[160px] max-w-[220px]" aria-label="Document type">
          <option value="">All types</option>
          {facets?.doc_type.map((f) => (
            <option key={f.value} value={f.value}>
              {docTypeLabel(f.value)} ({f.count})
            </option>
          ))}
        </Select>
      </div>

      <div className="px-6 pb-10 lg:px-10">
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {asFolders && data && data.total > 0 ? (
          <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5" aria-label="Projects">
            {[
              ...(facets?.project ?? []).map((f) => ({ value: f.value, count: f.count })),
              ...(facets?.unassigned ? [{ value: NO_PROJECT, count: facets.unassigned }] : []),
            ].map((f) => (
              <button
                key={f.value}
                type="button"
                onClick={() => openLibrary({ project: f.value, all: false })}
                className="group flex items-center gap-3 rounded-2xl border border-border px-4 py-3.5 text-left transition-colors hover:bg-surface-hover"
              >
                <Folder size={26} strokeWidth={1.5} className="shrink-0 text-muted group-hover:hidden" aria-hidden />
                <FolderOpen size={26} strokeWidth={1.5} className="hidden shrink-0 text-foreground group-hover:block" aria-hidden />
                <span className="min-w-0">
                  <span className="block truncate text-[14px] font-medium">{folderName(f.value)}</span>
                  <span className="block text-[12px] text-muted">
                    {f.count} file{f.count === 1 ? "" : "s"}
                  </span>
                </span>
              </button>
            ))}
            <button
              type="button"
              onClick={() => openLibrary({ project: "", all: true })}
              className="flex items-center gap-3 rounded-2xl border border-dashed border-border-strong px-4 py-3.5 text-left transition-colors hover:bg-surface-hover"
            >
              <FolderOpen size={26} strokeWidth={1.5} className="shrink-0 text-muted" aria-hidden />
              <span className="min-w-0">
                <span className="block truncate text-[14px] font-medium">All files</span>
                <span className="block text-[12px] text-muted">{total} in total</span>
              </span>
            </button>
          </div>
        ) : null}
        {!asFolders && !debounced && (project || showAll) ? (
          <div className="mb-3 flex items-center gap-2 text-[13px]">
            <Button
              size="sm"
              variant="ghost"
              icon={<ArrowLeft size={14} aria-hidden />}
              onClick={() => openLibrary({ project: "", all: false })}
            >
              All folders
            </Button>
            <span className="text-muted">/</span>
            <span className="font-medium">{project ? folderName(project) : "All files"}</span>
          </div>
        ) : null}
        {!data && loading ? <Spinner label="Loading documents" /> : null}
        {asFolders && data && data.total === 0 ? (
          <EmptyState
            title="No documents yet"
            action={
              <Button variant="primary" icon={<FileUp size={15} aria-hidden />} onClick={() => setUploading(true)}>
                Upload documents
              </Button>
            }
          >
            Upload files here, or ask an administrator to connect a SharePoint site, OneDrive or file share.
          </EmptyState>
        ) : null}
        {!asFolders && data && !data.documents.length ? (
          filtered ? (
            <EmptyState title="No documents match">Try fewer words, or clear the project and type filters.</EmptyState>
          ) : (
            <EmptyState
              title="No documents yet"
              action={
                <Button variant="primary" icon={<FileUp size={15} aria-hidden />} onClick={() => setUploading(true)}>
                  Upload documents
                </Button>
              }
            >
              Upload files here, or ask an administrator to connect a SharePoint site, OneDrive or file share.
            </EmptyState>
          )
        ) : null}
        {!asFolders && data && data.documents.length ? (
          <div className="rounded-2xl border border-border">
            <div className="flex min-h-[44px] flex-wrap items-center gap-2 border-b border-border px-4 py-2 text-[13px]">
              <Button size="sm" variant="ghost" onClick={allSelected ? clearSelection : selectAll}>
                {allSelected ? "Deselect all" : "Select all"}
              </Button>
              {selected.size ? (
                <>
                  <span className="text-muted">{selected.size} selected</span>
                  <Button
                    size="sm"
                    variant="danger"
                    icon={<Trash2 size={14} aria-hidden />}
                    onClick={() =>
                      setDeleting({ ids: [...selected], label: `${selected.size} selected document${selected.size === 1 ? "" : "s"} will be deleted.` })
                    }
                  >
                    Delete selected
                  </Button>
                </>
              ) : (
                <span className="text-muted">Tick files to delete several at once.</span>
              )}
            </div>
            <Table>
              <thead>
                <tr>
                  <Th className="w-10 !pr-0">
                    <input
                      ref={master}
                      type="checkbox"
                      checked={allSelected}
                      onChange={() => (allSelected ? clearSelection() : selectAll())}
                      aria-label={allSelected ? "Deselect all documents" : "Select all documents"}
                      className="h-3.5 w-3.5 accent-[var(--accent)]"
                    />
                  </Th>
                  <Th className="w-12">S.No</Th>
                  <Th>Document</Th>
                  <Th>Type</Th>
                  <Th>Project</Th>
                  <Th align="right">Pages</Th>
                  <Th>Added</Th>
                  <Th className="w-[120px]">
                    <span className="sr-only">Actions</span>
                  </Th>
                </tr>
              </thead>
              <tbody>
                {data.documents.map((d, index) => (
                  <tr
                    key={d.id}
                    tabIndex={0}
                    onClick={() => openDocument(d.id)}
                    onKeyDown={(e) => e.key === "Enter" && openDocument(d.id)}
                    className="cursor-pointer transition-colors hover:bg-surface-hover focus-visible:bg-surface-hover"
                  >
                    <Td className="w-10 !pr-0">
                      <input
                        type="checkbox"
                        checked={selected.has(d.id)}
                        onClick={(e) => e.stopPropagation()}
                        onChange={() => toggle(d.id)}
                        aria-label={`Select ${d.doc_number || d.file_name || d.title}`}
                        className="h-3.5 w-3.5 accent-[var(--accent)]"
                      />
                    </Td>
                    <Td className="w-12 text-muted tabular">{offset + index + 1}</Td>
                    <Td className="max-w-[420px]">
                      <div className="truncate font-medium tabular">{d.doc_number || d.file_name || d.title}</div>
                      <div dir="auto" className="truncate text-[12.5px] text-muted">
                        {d.doc_number ? d.title : d.file_name !== d.title ? d.title : ""}
                      </div>
                    </Td>
                    <Td className="whitespace-nowrap">{docTypeLabel(d.doc_type)}</Td>
                    <Td className="max-w-[180px] truncate">{d.project_ids.join(", ") || <span className="text-muted">—</span>}</Td>
                    <Td align="right">{d.page_count}</Td>
                    <Td className="whitespace-nowrap text-muted">{shortDate(d.updated_at)}</Td>
                    <Td className="w-[120px] !px-2">
                      <div onClick={(e) => e.stopPropagation()} onKeyDown={(e) => e.stopPropagation()}>
                        <RowActions
                          title={d.doc_number || d.file_name || d.title}
                          onEdit={() => setEditing(d)}
                          onReprocess={() => setReprocessing(d)}
                          onDelete={() => setDeleting({ ids: [d.id], label: `“${d.file_name || d.title}” will be deleted.` })}
                        />
                      </div>
                    </Td>
                  </tr>
                ))}
              </tbody>
            </Table>
            <div className="flex items-center justify-between px-4 py-2.5 text-[12.5px] text-muted">
              <span className="tabular">
                {offset + 1}–{offset + data.documents.length} of {total}
              </span>
              <span className="flex items-center gap-1">
                <IconButton label="Previous page" onClick={() => setOffset(Math.max(0, offset - PAGE))} disabled={offset === 0}>
                  <ChevronLeft size={16} />
                </IconButton>
                <IconButton label="Next page" onClick={() => setOffset(offset + PAGE)} disabled={offset + PAGE >= total}>
                  <ChevronRight size={16} />
                </IconButton>
              </span>
            </div>
          </div>
        ) : null}
      </div>
      {editing ? (
        <EditProjectDialog
          id={editing.id}
          title={editing.file_name || editing.title}
          current={editing.project_ids[0] ?? ""}
          projects={facets?.project.map((p) => p.value) ?? []}
          onClose={() => setEditing(null)}
          onSaved={onUploaded}
        />
      ) : null}
      {reprocessing ? (
        <ReprocessDialog
          id={reprocessing.id}
          title={reprocessing.file_name || reprocessing.title}
          onClose={() => setReprocessing(null)}
          onQueued={onUploaded}
        />
      ) : null}
      {deleting ? <DeleteDialog ids={deleting.ids} label={deleting.label} onClose={() => setDeleting(null)} onDeleted={afterChange} /> : null}
      {uploading ? <UploadDialog onClose={() => setUploading(false)} onUploaded={onUploaded} /> : null}
    </div>
  );
}

export default function DocumentsRoute() {
  return (
    <Suspense fallback={<div className="px-6 py-8 lg:px-10"><Spinner label="Loading documents" /></div>}>
      <DocumentsPage />
    </Suspense>
  );
}
