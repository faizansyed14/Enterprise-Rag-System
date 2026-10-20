"use client";

import clsx from "clsx";
import { CircleCheck, CircleX, Pencil, PlayCircle, Plus, RefreshCw, Trash2 } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import { BrandIcon, providerName } from "@/components/brand/brand";
import { Button } from "@/components/ui/button";
import { Badge, EmptyState, ErrorNote, Spinner, StatusBadge } from "@/components/ui/feedback";
import { Field, Input, Switch, Textarea } from "@/components/ui/fields";
import { PageHeader } from "@/components/ui/layout";
import { Dialog } from "@/components/ui/overlay";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { compact, relativeTime } from "@/lib/format";
import { useApi } from "@/lib/hooks";

type Source = {
  id: string;
  name: string;
  kind: string;
  config: Record<string, unknown>;
  enabled: boolean;
  schedule_minutes: number | null;
  default_acl: string[];
  last_sync_started_at: string | null;
  last_sync_finished_at: string | null;
  last_sync_status: string | null;
  last_sync_error: string | null;
  stats: Record<string, number> | null;
  items: Record<string, number>;
};

type FieldSpec = { key: string; label: string; hint?: string; placeholder?: string; list?: boolean; json?: boolean; required?: boolean };
type Kind = { kind: string; title: string; adding: string; summary: string; fields: FieldSpec[] };

// What each kind of source needs. Secrets are never typed here: they live in .env and are referenced by name.
const KINDS: Kind[] = [
  {
    kind: "sharepoint",
    title: "SharePoint site",
    adding: "Add a SharePoint site",
    summary: "Document libraries of one site, with their permissions. Uses the Entra app in .env (ENTRA_TENANT_ID, ENTRA_CLIENT_ID, ENTRA_CLIENT_SECRET).",
    fields: [
      { key: "site_url", label: "Site address", placeholder: "https://contoso.sharepoint.com/sites/Projects", required: true },
      { key: "drives", label: "Libraries", hint: "Optional, one per line. Leave empty for every library on the site.", list: true, placeholder: "Documents" },
    ],
  },
  {
    kind: "onedrive",
    title: "OneDrive",
    adding: "Add a OneDrive",
    summary: "One person's OneDrive, with sharing permissions. Uses the same Entra app.",
    fields: [{ key: "user", label: "Owner", hint: "User principal name or object id", placeholder: "alex@contoso.com", required: true }],
  },
  {
    kind: "smb",
    title: "Windows file share",
    adding: "Add a Windows file share",
    summary: "A folder on an SMB share. The password is read from the environment variable you name, never stored here.",
    fields: [
      { key: "server", label: "Server", placeholder: "fileserver01", required: true },
      { key: "share", label: "Share", placeholder: "Projects", required: true },
      { key: "path", label: "Folder in the share", placeholder: "Marina Tower/Issued" },
      { key: "username", label: "User", placeholder: "CONTOSO\\svc-rag" },
      { key: "password_env", label: "Password variable", hint: "Name of the environment variable that holds the password", placeholder: "SMB_PASSWORD" },
      { key: "include", label: "Include", hint: "Optional glob patterns, one per line", list: true, placeholder: "**/*.pdf" },
      { key: "exclude", label: "Exclude", hint: "Optional glob patterns, one per line", list: true, placeholder: "**/Superseded/**" },
    ],
  },
  {
    kind: "filesystem",
    title: "Server folder",
    adding: "Add a server folder",
    summary: "A folder mounted into the worker containers (for example a NAS mount).",
    fields: [
      { key: "root", label: "Folder", placeholder: "/data/projects", required: true },
      { key: "include", label: "Include", hint: "Optional glob patterns, one per line", list: true, placeholder: "**/*" },
      { key: "exclude", label: "Exclude", hint: "Optional glob patterns, one per line", list: true, placeholder: "**/Archive/**" },
    ],
  },
];

// Every other connector (object stores, file servers, cloud drives, wikis, tickets, code, chat, mail, databases, web sites):
// its configuration as JSON. docs/connectors.md lists each kind's keys.
const OTHER: Kind = {
  kind: "other",
  title: "Another system",
  adding: "Add another system",
  summary:
    "S3 and compatible stores, Azure Blob, Google Cloud Storage, WebDAV, SFTP, FTP, Google Drive, Dropbox, Box, Confluence, Jira, Notion, Slack, Teams, GitHub, GitLab, Zendesk, mailboxes, SQL tables and web sites. Secrets are named environment variables (token_env, password_env ...), never typed here; docs/connectors.md lists each kind's settings.",
  fields: [
    { key: "__kind", label: "Kind", hint: "For example s3, azure_blob, gcs, webdav, sftp, gdrive, confluence, web", placeholder: "s3", required: true },
    {
      key: "__json",
      label: "Configuration (JSON)",
      json: true,
      required: true,
      placeholder: '{"bucket": "projects", "prefix": "issued/", "access_key_env": "S3_ACCESS_KEY", "secret_key_env": "S3_SECRET_KEY"}',
    },
  ],
};

function describe(source: Source): string {
  const c = source.config as Record<string, string>;
  switch (source.kind) {
    case "sharepoint":
      return c.site_url || c.site_id || "";
    case "onedrive":
      return c.user || "";
    case "smb":
      return `\\\\${c.server}\\${c.share}${c.path ? `\\${c.path}` : ""}`;
    case "filesystem":
      return c.root || "";
    case "upload":
      return "Files people upload in the app";
    default:
      return [c.bucket, c.container, c.prefix].filter(Boolean).join("/") || c.url || c.base_url || c.site || c.host || "";
  }
}

function syncState(source: Source): ReactNode {
  if (!source.enabled) return <Badge tone="outline">Off</Badge>;
  const status = source.last_sync_status;
  if (status === "running") return <StatusBadge state="info">Syncing</StatusBadge>;
  if (status === "ok") return <StatusBadge state="ok">Synced {relativeTime(source.last_sync_finished_at)}</StatusBadge>;
  if (status === "error") return <StatusBadge state="fail">Sync failed</StatusBadge>;
  if (source.kind === "upload") return <StatusBadge state="ok">Ready</StatusBadge>;
  return <StatusBadge state="info">Not synced yet</StatusBadge>;
}

function SourceRow({ source, onEdit, onChanged }: { source: Source; onEdit: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [test, setTest] = useState<{ ok: boolean; detail: string } | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const items = source.items || {};
  const active = items.active ?? 0;
  const failed = (items.error ?? 0) + (items.skipped ?? 0);

  const act = async (name: string, fn: () => Promise<void>) => {
    setBusy(name);
    try {
      await fn();
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="flex flex-wrap items-start gap-x-4 gap-y-3 border-b border-border px-5 py-4 last:border-b-0">
      <BrandIcon name={source.kind} size={40} />
      <div className="min-w-[240px] flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[14px] font-semibold">{source.name}</span>
          <span className="text-[12px] text-muted">{providerName(source.kind)}</span>
          {syncState(source)}
        </div>
        <div className="mt-0.5 truncate text-[12.5px] text-muted tabular">{describe(source)}</div>
        <div className="mt-2 flex flex-wrap gap-x-5 gap-y-1 text-[12.5px]">
          <span>
            <span className="tabular font-medium">{compact(active)}</span> <span className="text-muted">{active === 1 ? "file" : "files"}</span>
          </span>
          {failed ? (
            <span className="text-warning">
              <span className="tabular font-medium">{compact(failed)}</span> could not be read
            </span>
          ) : null}
          {source.schedule_minutes && source.kind !== "upload" ? (
            <span className="text-muted">Every {source.schedule_minutes >= 60 ? `${Math.round(source.schedule_minutes / 60)} h` : `${source.schedule_minutes} min`}</span>
          ) : null}
          <span className="text-muted">Default access: {source.default_acl.join(", ") || "only listed permissions"}</span>
        </div>
        {source.last_sync_error ? <p className="mt-1.5 text-[12.5px] text-danger">{source.last_sync_error}</p> : null}
        {test ? (
          <p className={clsx("mt-1.5 flex items-center gap-1.5 text-[12.5px]", test.ok ? "text-success" : "text-danger")}>
            {test.ok ? <CircleCheck size={13} aria-hidden /> : <CircleX size={13} aria-hidden />}
            {test.detail}
          </p>
        ) : null}
        {confirm ? (
          <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg border border-danger/40 bg-danger-soft px-3 py-2 text-[12.5px] text-danger">
            Remove this source? Its {compact(active)} files leave search right away. The files themselves are not touched.
            <Button
              size="sm"
              variant="danger"
              onClick={() =>
                act("remove", async () => {
                  const r = await api<{ items_removed: number }>(`/sources/${source.id}`, { method: "DELETE" });
                  toast(`Removed; ${r.items_removed} files are leaving search`);
                  onChanged();
                })
              }
            >
              Remove
            </Button>
            <Button size="sm" variant="ghost" onClick={() => setConfirm(false)}>
              Keep
            </Button>
          </div>
        ) : null}
      </div>
      {source.kind !== "upload" ? (
        <div className="flex flex-wrap items-center gap-1.5">
          <Switch
            label={source.enabled ? "Turn off syncing" : "Turn on syncing"}
            checked={source.enabled}
            onChange={(enabled) =>
              act("toggle", async () => {
                await api(`/sources/${source.id}`, { method: "PATCH", body: { enabled } });
                onChanged();
              })
            }
          />
          <Button
            size="sm"
            variant="ghost"
            disabled={busy !== null}
            icon={<PlayCircle size={14} aria-hidden />}
            onClick={() =>
              act("test", async () => {
                setTest(null);
                setTest(await api<{ ok: boolean; detail: string }>(`/sources/${source.id}/test`, { method: "POST" }));
              })
            }
          >
            {busy === "test" ? "Testing" : "Test"}
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={busy !== null || !source.enabled}
            icon={<RefreshCw size={14} aria-hidden />}
            onClick={() =>
              act("sync", async () => {
                await api(`/sources/${source.id}/sync`, { method: "POST" });
                toast("Sync queued ahead of bulk work");
                onChanged();
              })
            }
          >
            Sync now
          </Button>
          <Button size="sm" variant="ghost" icon={<Pencil size={14} aria-hidden />} onClick={onEdit}>
            Edit
          </Button>
          <Button size="sm" variant="ghost" icon={<Trash2 size={14} aria-hidden />} onClick={() => setConfirm(true)} aria-label={`Remove ${source.name}`}>
            Remove
          </Button>
        </div>
      ) : null}
    </div>
  );
}

/** Mounted while open (keyed by the source being edited), so the fields start from that source. */
function SourceDialog({ source, onClose, onSaved }: { source: Source | null; onClose: () => void; onSaved: () => void }) {
  const toast = useToast();
  const native = source ? KINDS.find((k) => k.kind === source.kind) : undefined;
  const [kind, setKind] = useState<Kind | null>(source ? native ?? OTHER : null);
  const [name, setName] = useState(source?.name ?? "");
  const [values, setValues] = useState<Record<string, string>>(
    !source
      ? {}
      : native
        ? Object.fromEntries(Object.entries(source.config).map(([k, v]) => [k, Array.isArray(v) ? v.join("\n") : String(v ?? "")]))
        : { __kind: source.kind, __json: JSON.stringify(source.config, null, 2) },
  );
  const [acl, setAcl] = useState(source ? source.default_acl.join(", ") : "everyone");
  const [schedule, setSchedule] = useState(String(source?.schedule_minutes ?? 60));
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const save = async () => {
    if (!kind) return;
    let config: Record<string, unknown> = {};
    let kindName = kind.kind;
    if (kind === OTHER) {
      kindName = (values.__kind ?? "").trim();
      try {
        const parsed: unknown = JSON.parse(values.__json ?? "");
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
        config = parsed as Record<string, unknown>;
      } catch {
        setError("The configuration is not a JSON object, for example {\"bucket\": \"projects\"}");
        return;
      }
    } else {
      for (const field of kind.fields) {
        const raw = (values[field.key] ?? "").trim();
        if (!raw) continue;
        config[field.key] = field.list ? raw.split("\n").map((s) => s.trim()).filter(Boolean) : raw;
      }
    }
    const body = {
      name: name.trim(),
      config,
      default_acl: acl.split(",").map((s) => s.trim()).filter(Boolean),
      schedule_minutes: Number(schedule) || 60,
    };
    setBusy(true);
    setError(null);
    try {
      if (source) await api(`/sources/${source.id}`, { method: "PATCH", body });
      else await api("/sources", { body: { ...body, kind: kindName } });
      toast(source ? "Source saved" : "Source added; start the first sync with Sync now");
      onSaved();
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  const missing = kind?.fields.some((f) => f.required && !(values[f.key] ?? "").trim()) || !name.trim();

  return (
    <Dialog
      open
      onClose={onClose}
      wide
      title={source ? `Edit ${source.name}` : kind ? kind.adding : "Add a source"}
      footer={
        kind ? (
          <>
            {!source ? (
              <Button variant="ghost" onClick={() => setKind(null)}>
                Back
              </Button>
            ) : null}
            <Button onClick={onClose}>Cancel</Button>
            <Button variant="primary" onClick={save} disabled={busy || missing}>
              {busy ? "Saving" : source ? "Save" : "Add source"}
            </Button>
          </>
        ) : undefined
      }
    >
      {!kind ? (
        <div className="grid gap-2.5 sm:grid-cols-2">
          {[...KINDS, OTHER].map((k) => (
            <button
              key={k.kind}
              type="button"
              onClick={() => {
                setKind(k);
                if (!name) setName(k.title);
              }}
              className="flex items-start gap-3 rounded-xl border border-border p-4 text-left transition-colors hover:border-border-strong hover:bg-surface-hover"
            >
              <BrandIcon name={k.kind} size={40} />
              <span className="min-w-0">
                <span className="block text-[14px] font-medium">{k.title}</span>
                <span className="mt-0.5 block text-[12.5px] leading-snug text-muted">{k.summary}</span>
              </span>
            </button>
          ))}
        </div>
      ) : (
        <>
          <div className="flex items-start gap-3 rounded-xl bg-surface px-4 py-3">
            <BrandIcon name={kind.kind} size={32} />
            <p className="text-[12.5px] leading-snug text-muted">{kind.summary}</p>
          </div>
          <Field label="Name" hint="Shown to administrators and in sync logs">
            <Input value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          {kind.fields.map((f) => (
            <Field key={f.key} label={f.required ? f.label : `${f.label} (optional)`} hint={f.hint}>
              {f.json ? (
                <Textarea
                  rows={8}
                  className="font-mono text-[12.5px]"
                  value={values[f.key] ?? ""}
                  placeholder={f.placeholder}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                />
              ) : f.list ? (
                <Textarea rows={2} value={values[f.key] ?? ""} placeholder={f.placeholder} onChange={(e) => setValues({ ...values, [f.key]: e.target.value })} />
              ) : (
                <Input
                  value={values[f.key] ?? ""}
                  placeholder={f.placeholder}
                  disabled={Boolean(source) && f.key === "__kind"}
                  onChange={(e) => setValues({ ...values, [f.key]: e.target.value })}
                />
              )}
            </Field>
          ))}
          <div className="grid gap-4 sm:grid-cols-2">
            <Field label="Default access" hint="Used for files without their own permissions. Comma-separated: everyone, lgroup:name, group:<entra id>">
              <Input value={acl} onChange={(e) => setAcl(e.target.value)} />
            </Field>
            <Field label="Sync every (minutes)" hint="Changes are fetched incrementally">
              <Input type="number" min={5} max={10080} value={schedule} onChange={(e) => setSchedule(e.target.value)} />
            </Field>
          </div>
          {error ? <ErrorNote>{error}</ErrorNote> : null}
        </>
      )}
    </Dialog>
  );
}

export default function SourcesPage() {
  const { data, error, reload } = useApi<{ sources: Source[]; kinds: string[] }>("/sources", { refreshMs: 15_000 });
  const [dialog, setDialog] = useState<{ open: boolean; source: Source | null }>({ open: false, source: null });

  useEffect(() => {
    document.title = "Sources";
  }, []);

  const sources = data?.sources ?? [];
  return (
    <div>
      <PageHeader
        title="Sources"
        description="Where documents come from. Each source keeps its own permissions; people only find what they can open at the source."
        actions={
          <Button variant="primary" icon={<Plus size={15} aria-hidden />} onClick={() => setDialog({ open: true, source: null })}>
            Add source
          </Button>
        }
      />
      <div className="px-6 py-6 lg:px-10">
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading sources" /> : null}
        {data && !sources.length ? (
          <EmptyState
            title="No sources connected"
            action={
              <Button variant="primary" icon={<Plus size={15} aria-hidden />} onClick={() => setDialog({ open: true, source: null })}>
                Add source
              </Button>
            }
          >
            Connect a SharePoint site, a OneDrive, a Windows file share or a server folder. Uploads work without any source.
          </EmptyState>
        ) : null}
        {sources.length ? (
          <div className="rounded-2xl border border-border bg-surface-raised">
            {sources.map((s) => (
              <SourceRow key={s.id} source={s} onEdit={() => setDialog({ open: true, source: s })} onChanged={reload} />
            ))}
          </div>
        ) : null}
      </div>
      {dialog.open ? <SourceDialog key={dialog.source?.id ?? "new"} source={dialog.source} onClose={() => setDialog({ open: false, source: null })} onSaved={reload} /> : null}
    </div>
  );
}
