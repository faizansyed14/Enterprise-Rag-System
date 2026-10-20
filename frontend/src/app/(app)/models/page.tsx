"use client";

import { AlertTriangle, Check, CircleCheck, CircleX, History, PlayCircle, Power, RefreshCw, RotateCcw, Undo2 } from "lucide-react";
import { useEffect, useState } from "react";

import { BrandIcon, ModelIcon, providerName, splitSpec } from "@/components/brand/brand";
import { Button } from "@/components/ui/button";
import { Badge, ErrorNote, Meter, Spinner, StatusBadge } from "@/components/ui/feedback";
import { Field, Input, Select } from "@/components/ui/fields";
import { GridFillers, PageHeader, Section, Table, Td, Th } from "@/components/ui/layout";
import { Dialog } from "@/components/ui/overlay";
import { useToast } from "@/components/ui/toast";
import { api, errorMessage } from "@/lib/api";
import { compact, relativeTime, shortDate } from "@/lib/format";
import { useApi } from "@/lib/hooks";
import { ROLE_NAMES } from "@/lib/roles";
import { useSession } from "@/lib/session";
import type { RoleRow } from "@/lib/types";

type LocalModel = { name: string; kind: string; dims: number | null; max_tokens: number; languages: string[] | null; downloaded: boolean; loaded: boolean; notes: string };
type Provider = { capability: string; provider: string; description: string; local: boolean; requires: string[]; configured: boolean };
type ModelsData = {
  roles: RoleRow[];
  choices: string[];
  providers: Provider[];
  local_server: { url: string; reachable: boolean; models: LocalModel[]; error?: string };
  allow_switch: boolean;
  show_badge: boolean;
  zdr: boolean;
};
type Build = { id: string; collection: string; embedder: string; dims: number; analyzer: string; chunker_version: string; status: string; created_at: string; live_at: string | null; total_versions: number; done_versions: number; points: number | null };
type IndexData = { configured: { collection: string; embedder: string; embedder_dims: number }; live: Build | null; builds: Build[]; live_versions: number; rebuild_needed: boolean };
type Switch = { role: string; from: string; to: string; by: string | null; reason: string | null; at: string };


const GROUPS: { title: string; description: string; roles: string[] }[] = [
  { title: "Answering", description: "The AI models that write answers from the passages search finds. Choose any provider and model from the list, or turn one off.", roles: ["LLM_ANSWER", "LLM_ANSWER_HARD", "LLM_ANSWER_FALLBACK", "LLM_ROUTER"] },
  { title: "Reading documents", description: "Used when files are uploaded. Scanned pages are read on this server for free by default; choose the vision model instead for better tables, handwriting and pictures (it costs a model call per page). Vision and metadata filling are optional extras that send pages or text to the model you pick.", roles: ["PARSER", "OCR", "VISION", "LLM_ENRICH"] },
  { title: "Search", description: "Runs on this server and is set in .env, because changing it means re-indexing your documents.", roles: ["EMBEDDER", "RERANKER", "GROUNDEDNESS"] },
];

type TestResult = { ok: boolean; detail: string; ms: number };

function RoleRowView({ row, isAdmin, onSwitch, onChanged }: { row: RoleRow; isAdmin: boolean; onSwitch: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [testing, setTesting] = useState(false);
  const [result, setResult] = useState<TestResult | null>(null);
  const off = row.provider === "none";

  const test = async () => {
    setTesting(true);
    setResult(null);
    try {
      setResult(await api<TestResult>("/models/test", { body: { role: row.role } }));
    } catch (e) {
      setResult({ ok: false, detail: errorMessage(e), ms: 0 });
    } finally {
      setTesting(false);
    }
  };
  const reset = async () => {
    try {
      await api(`/models/${row.role}`, { method: "DELETE" });
      toast(`${ROLE_NAMES[row.role] ?? row.role} uses ${row.env_label} again`);
      onChanged();
    } catch (e) {
      toast(errorMessage(e), "error");
    }
  };

  return (
    <div className="flex flex-wrap items-start gap-x-4 gap-y-3 border-b border-border px-5 py-4 last:border-b-0">
      {row.capability === "chat" || row.capability === "vision" ? <ModelIcon provider={row.provider} model={row.model} size={36} /> : <BrandIcon name={row.provider} size={36} />}
      <div className="min-w-[220px] flex-1">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[14px] font-semibold">{ROLE_NAMES[row.role] ?? row.role}</span>
          <code className="text-[11.5px] text-muted">{row.role}</code>
          {off ? <Badge>Off</Badge> : row.local ? <Badge tone="outline">Runs here</Badge> : <Badge tone="outline">Cloud</Badge>}
          {row.overridden ? (
            <Badge tone="warning" title={`.env says ${row.env_label}`}>
              Changed in the app
            </Badge>
          ) : null}
        </div>
        <p className="mt-0.5 text-[12.5px] text-muted">{row.description}</p>
        <div className="mt-2 flex flex-wrap items-center gap-2 text-[13px]">
          <span className="tabular">{off ? "Switched off" : row.label}</span>
          {!off ? <span className="text-[12px] text-muted">{row.provider === "local" && (row.capability === "parser" || row.capability === "ocr") ? "Runs in the workers" : providerName(row.provider)}</span> : null}
        </div>
        {row.missing.length ? (
          <p className="mt-1.5 flex items-center gap-1.5 text-[12.5px] text-warning">
            <AlertTriangle size={13} aria-hidden />
            Needs {row.missing.join(", ")} in .env
          </p>
        ) : null}
        {!row.known_provider ? (
          <p className="mt-1.5 flex items-center gap-1.5 text-[12.5px] text-danger">
            <CircleX size={13} aria-hidden />
            No provider named {row.provider} for this role
          </p>
        ) : null}
        {result ? (
          <p className={`mt-1.5 flex items-center gap-1.5 text-[12.5px] ${result.ok ? "text-success" : "text-danger"}`}>
            {result.ok ? <CircleCheck size={13} aria-hidden /> : <CircleX size={13} aria-hidden />}
            <span className="min-w-0 break-words">
              {result.detail}
              {result.ms ? <span className="text-muted"> in {result.ms} ms</span> : null}
            </span>
          </p>
        ) : null}
      </div>
      {isAdmin ? (
        <div className="flex items-center gap-1.5">
          {!off ? (
            <Button size="sm" variant="ghost" onClick={test} disabled={testing} icon={<PlayCircle size={14} aria-hidden />}>
              {testing ? "Testing" : "Test"}
            </Button>
          ) : null}
          {row.overridden ? (
            <Button size="sm" variant="ghost" onClick={reset} icon={<Undo2 size={14} aria-hidden />}>
              Use .env
            </Button>
          ) : null}
          {row.switchable ? (
            <Button size="sm" variant={off ? "primary" : "secondary"} onClick={onSwitch} icon={off ? <Power size={14} aria-hidden /> : <RefreshCw size={14} aria-hidden />}>
              {off ? "Turn on" : "Change"}
            </Button>
          ) : (
            <span className="text-[12px] text-muted" title="Changing this means re-indexing or re-reading documents, so it is set in .env">
              Set in .env
            </span>
          )}
        </div>
      ) : null}
    </div>
  );
}

type ModelOption = { id: string; name: string; free?: boolean; vision?: boolean; prompt_per_m?: number | null; completion_per_m?: number | null };
type ProviderOption = { provider: string; description: string; local: boolean; requires: string[]; configured: boolean; models: ModelOption[]; custom: boolean };
type RoleOptions = { role: string; current: string; providers: ProviderOption[]; catalogue_available: boolean; zdr: boolean; images_required: boolean };

const OFF = "none";
const MAX_LISTED = 80;

function priceText(m: ModelOption): string {
  if (m.free) return "Free";
  if (m.prompt_per_m == null || m.completion_per_m == null) return "";
  return `$${m.prompt_per_m} in / $${m.completion_per_m} out per 1M tokens`;
}

/** Mounted per role while open. Providers and models come from the server, so nothing is typed by hand. */
function SwitchDialog({ row, onClose, onDone }: { row: RoleRow; onClose: () => void; onDone: () => void }) {
  const toast = useToast();
  const [options, setOptions] = useState<RoleOptions | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [provider, setProvider] = useState(row.provider);
  const [model, setModel] = useState(row.provider === OFF ? "" : row.model);
  const [query, setQuery] = useState("");
  const [freeOnly, setFreeOnly] = useState(false);
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api<RoleOptions>(`/models/options/${row.role}`)
      .then(setOptions)
      .catch((e) => setLoadError(errorMessage(e)));
  }, [row.role]);

  const entry = options?.providers.find((p) => p.provider === provider);
  const off = provider === OFF;
  const needle = query.trim().toLowerCase();
  const shown = (entry?.models ?? []).filter((m) => (!freeOnly || m.free) && (!needle || m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle)));
  const value = off ? OFF : provider && model.trim() ? `${provider}/${model.trim()}` : "";
  const parts = value && !off ? splitSpec(value) : null;
  const picked = entry?.models.find((m) => m.id === model);
  const freeBlocked = Boolean(options?.zdr && picked?.free && provider === "openrouter");

  const pickProvider = (next: string) => {
    setProvider(next);
    setQuery("");
    const only = options?.providers.find((p) => p.provider === next)?.models;
    setModel(next === OFF ? "" : only && only.length === 1 ? only[0].id : next === row.provider ? row.model : "");
  };

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await api<{ spec: string }>(`/models/${row.role}`, { method: "PUT", body: { spec: value, reason } });
      toast(off ? `${ROLE_NAMES[row.role]} switched off` : `${ROLE_NAMES[row.role]} now uses ${result.spec}`);
      onDone();
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog
      open
      onClose={onClose}
      wide
      title={`${row.provider === OFF ? "Turn on" : "Change"}: ${ROLE_NAMES[row.role]?.toLowerCase() ?? row.role}`}
      footer={
        <>
          <Button onClick={onClose}>Cancel</Button>
          <Button variant="primary" onClick={save} disabled={busy || !value || value === row.label || freeBlocked}>
            {busy ? "Saving" : off ? "Switch off" : "Use this model"}
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-muted">{row.description}. Takes effect for the next {row.capability === "vision" || row.role === "LLM_ENRICH" || row.role === "OCR" ? "file that is uploaded" : "question"}; it is recorded in the history, and <span className="tabular">Use .env</span> undoes it.</p>
      {loadError ? <ErrorNote>{loadError}</ErrorNote> : null}
      {!options && !loadError ? <Spinner label="Loading providers and models" /> : null}
      {options ? (
        <>
          <Field label="Provider">
            <Select value={provider} onChange={(e) => pickProvider(e.target.value)}>
              <option value={OFF}>Off</option>
              {options.providers.map((p) => (
                <option key={p.provider} value={p.provider} disabled={!p.configured}>
                  {providerName(p.provider)}
                  {p.local ? " (runs here, free)" : ""}
                  {!p.configured ? ` — needs ${p.requires.join(", ")} in .env` : ""}
                </option>
              ))}
            </Select>
          </Field>
          {entry && !entry.custom ? (
            <Field label={options.images_required ? "Model (only those marked Text + images can be chosen)" : "Model"} hint={entry.models.length > 1 ? `${shown.length} of ${entry.models.length} shown. Type to search.` : undefined}>
              <div className="space-y-1.5">
                {entry.models.length > 8 ? (
                  <div className="flex items-center gap-3">
                    <Input value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Search: claude, gpt, llama, gemini, qwen ..." aria-label="Search models" />
                    <label className="flex shrink-0 items-center gap-1.5 text-[12.5px] text-muted">
                      <input type="checkbox" checked={freeOnly} onChange={(e) => setFreeOnly(e.target.checked)} />
                      Free only
                    </label>
                  </div>
                ) : null}
                <div className="max-h-64 overflow-y-auto rounded-xl border border-border" role="listbox" aria-label="Models">
                  {shown.slice(0, MAX_LISTED).map((m) => {
                    const blocked = Boolean(options.images_required && provider === "openrouter" && !m.vision);
                    return (
                    <button
                      key={m.id}
                      type="button"
                      role="option"
                      aria-selected={m.id === model}
                      aria-disabled={blocked}
                      disabled={blocked}
                      title={blocked ? "This model reads text only, so it cannot read images or scanned pages" : undefined}
                      onClick={() => setModel(m.id)}
                      className={`flex w-full items-start gap-2.5 border-b border-border px-3 py-2 text-left last:border-b-0 ${blocked ? "opacity-50" : "hover:bg-surface-hover"}`}
                    >
                      <ModelIcon provider={provider} model={m.id} size={24} />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center gap-1.5">
                          <span className="min-w-0 truncate text-[13px] font-medium">{m.name}</span>
                          {provider === "openrouter" ? <Badge tone="outline">{m.vision ? "Text + images" : "Text only"}</Badge> : null}
                          {m.free ? <Badge tone="outline">Free</Badge> : null}
                        </span>
                        <span className="block truncate text-[12px] text-muted">
                          {m.id}
                          {!m.free && priceText(m) ? ` · ${priceText(m)}` : ""}
                          {blocked ? " · cannot be used here: it does not read images" : ""}
                        </span>
                      </span>
                      {m.id === model ? <Check size={15} className="mt-1 shrink-0" aria-label="Selected" /> : null}
                    </button>
                    );
                  })}
                  {!shown.length ? <p className="px-3 py-3 text-[12.5px] text-muted">No model matches.</p> : null}
                  {shown.length > MAX_LISTED ? <p className="px-3 py-2 text-[12px] text-muted">{shown.length - MAX_LISTED} more: narrow the search.</p> : null}
                </div>
              </div>
            </Field>
          ) : null}
          {entry?.custom ? (
            <Field label="Model" hint="This provider has no public list, so type the model id it gives you.">
              <Input value={model} onChange={(e) => setModel(e.target.value)} placeholder="model id" autoFocus />
            </Field>
          ) : null}
          {!options.catalogue_available && provider === "openrouter" ? <ErrorNote>OpenRouter&apos;s model list could not be loaded just now. Close this and try again.</ErrorNote> : null}
          {freeBlocked ? (
            <p className="flex items-start gap-1.5 rounded-xl border border-border bg-warning-soft px-3 py-2.5 text-[12.5px] text-warning">
              <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden />
              <span>Free models keep a copy of what they are sent, and zero data retention is switched on (OPENROUTER_ZDR=true in .env), so OpenRouter will refuse this model. Pick a paid model, or set OPENROUTER_ZDR=false if the documents are not confidential.</span>
            </p>
          ) : null}
          {off ? (
            <p className="rounded-xl border border-border px-3 py-2.5 text-[13px] text-muted">This job will be skipped. Nothing is sent to any model.</p>
          ) : parts ? (
            <div className="flex items-center gap-2.5 rounded-xl border border-border px-3 py-2.5">
              <ModelIcon provider={parts.provider} model={parts.model} size={28} />
              <div className="min-w-0 text-[13px]">
                <div className="truncate font-medium">{parts.model || parts.provider}</div>
                <div className="text-[12px] text-muted">Via {providerName(parts.provider)}</div>
              </div>
            </div>
          ) : null}
          <Field label="Reason" hint="Optional, shown in the history.">
            <Input value={reason} onChange={(e) => setReason(e.target.value)} placeholder="Comparing answer quality" />
          </Field>
        </>
      ) : null}
      {error ? <ErrorNote>{error}</ErrorNote> : null}
    </Dialog>
  );
}

function LocalServer({ data }: { data: ModelsData["local_server"] }) {
  return (
    <Section
      title="Local model server"
      description={data.url}
      actions={data.reachable ? <StatusBadge state="ok">Reachable</StatusBadge> : <StatusBadge state="fail">Not reachable</StatusBadge>}
    >
      {data.reachable ? (
        <Table>
          <thead>
            <tr>
              <Th>Model</Th>
              <Th>Kind</Th>
              <Th align="right">Dimensions</Th>
              <Th>State</Th>
            </tr>
          </thead>
          <tbody>
            {data.models.map((m) => (
              <tr key={m.name}>
                <Td>
                  <div className="flex items-center gap-2.5">
                    <BrandIcon name="onnx" size={24} />
                    <div className="min-w-0">
                      <div className="font-medium">{m.name}</div>
                      {m.notes ? <div className="truncate text-[12px] text-muted">{m.notes}</div> : null}
                    </div>
                  </div>
                </Td>
                <Td>{m.kind}</Td>
                <Td align="right">{m.dims ?? "—"}</Td>
                <Td>{m.loaded ? <StatusBadge state="ok">Loaded</StatusBadge> : m.downloaded ? <StatusBadge state="info">Downloaded</StatusBadge> : <Badge tone="outline">Not downloaded</Badge>}</Td>
              </tr>
            ))}
          </tbody>
        </Table>
      ) : (
        <div className="px-5 py-4 text-[13px] text-muted">
          Start it with <code className="text-foreground">scripts/start.sh</code> (Docker) or <code className="text-foreground">rag-model-server serve</code>. Roles that use <code>local/…</code> embedders and rerankers need it.
          {data.error ? <span className="block pt-1 text-[12px]">Last error: {data.error}</span> : null}
        </div>
      )}
    </Section>
  );
}

function IndexCard() {
  const toast = useToast();
  const { data, error, reload } = useApi<IndexData>("/index", { refreshMs: 10_000 });
  const [busy, setBusy] = useState(false);
  const run = async (path: string, body?: unknown) => {
    setBusy(true);
    try {
      const result = await api<Record<string, unknown>>(path, { method: "POST", body });
      toast(result.reason ? String(result.reason) : result.started ? "Rebuild started; search keeps using the live index until it finishes" : "Done");
      await reload();
    } catch (e) {
      toast(errorMessage(e), "error");
    } finally {
      setBusy(false);
    }
  };
  if (error) return <ErrorNote>{error}</ErrorNote>;
  if (!data) return <Spinner label="Loading the index" />;
  const building = data.builds.find((b) => b.status === "building");
  return (
    <Section
      title="Search index"
      description={data.live ? `${compact(data.live.points)} passages from ${compact(data.live_versions)} documents` : "No live index yet"}
      actions={
        data.builds.some((b) => b.status === "retired") ? (
          <Button size="sm" variant="ghost" onClick={() => run("/index/rollback")} disabled={busy} icon={<RotateCcw size={14} aria-hidden />}>
            Roll back
          </Button>
        ) : null
      }
    >
      {data.rebuild_needed ? (
        <div className="flex flex-wrap items-center gap-3 border-b border-border bg-warning-soft px-5 py-3 text-[13px] text-warning">
          <AlertTriangle size={15} aria-hidden />
          <span className="min-w-0 flex-1">
            .env asks for <span className="tabular">{data.configured.embedder}</span>, but search uses <span className="tabular">{data.live?.embedder}</span>. Rebuild to switch; search keeps working on the live index meanwhile.
          </span>
          {!building ? (
            <Button size="sm" onClick={() => run("/index/rebuild")} disabled={busy}>
              Rebuild index
            </Button>
          ) : null}
        </div>
      ) : null}
      <Table>
        <thead>
          <tr>
            <Th>Index</Th>
            <Th>Embedder</Th>
            <Th>State</Th>
            <Th align="right">Passages</Th>
            <Th>Since</Th>
          </tr>
        </thead>
        <tbody>
          {data.builds.map((b) => (
            <tr key={b.id}>
              <Td className="max-w-[300px]">
                <div className="truncate tabular" title={b.collection}>
                  {b.collection}
                </div>
                <div className="text-[12px] text-muted">
                  Analyzer {b.analyzer}, chunker {b.chunker_version}
                </div>
              </Td>
              <Td className="tabular">
                {b.embedder} <span className="text-muted">({b.dims})</span>
              </Td>
              <Td className="min-w-[150px]">
                {b.status === "live" ? (
                  <StatusBadge state="ok">Live</StatusBadge>
                ) : b.status === "building" ? (
                  <div className="space-y-1">
                    <span className="text-[12.5px]">
                      Building {b.done_versions} of {b.total_versions}
                    </span>
                    <Meter value={b.done_versions} max={b.total_versions || 1} label="Rebuild progress" />
                  </div>
                ) : (
                  <span className="flex items-center gap-2">
                    <Badge tone="outline">Retired</Badge>
                    <button type="button" onClick={() => run("/index/activate", { build_id: b.id })} disabled={busy} className="text-[12.5px] underline underline-offset-2">
                      Use again
                    </button>
                  </span>
                )}
              </Td>
              <Td align="right">{compact(b.points)}</Td>
              <Td className="whitespace-nowrap text-muted">{shortDate(b.live_at || b.created_at)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Section>
  );
}

function SwitchHistory() {
  const { data } = useApi<{ switches: Switch[] }>("/models/switches?limit=20");
  if (!data?.switches.length) return null;
  return (
    <Section title="Switch history" description="Every change made from this page, newest first.">
      <Table>
        <thead>
          <tr>
            <Th>Role</Th>
            <Th>From</Th>
            <Th>To</Th>
            <Th>By</Th>
            <Th>When</Th>
          </tr>
        </thead>
        <tbody>
          {data.switches.map((s, i) => (
            <tr key={i}>
              <Td>{ROLE_NAMES[s.role] ?? s.role}</Td>
              <Td className="max-w-[240px] truncate text-muted tabular">{s.from}</Td>
              <Td className="max-w-[240px] truncate tabular" title={s.reason ?? undefined}>
                {s.to}
              </Td>
              <Td className="text-muted">{s.by ?? "—"}</Td>
              <Td className="whitespace-nowrap text-muted">{relativeTime(s.at)}</Td>
            </tr>
          ))}
        </tbody>
      </Table>
    </Section>
  );
}

function Providers({ providers }: { providers: Provider[] }) {
  const byProvider = new Map<string, Provider[]>();
  for (const p of providers) byProvider.set(p.provider, [...(byProvider.get(p.provider) ?? []), p]);
  return (
    <Section title="Providers" description="What each provider can do, and whether its keys are in .env.">
      <div className="grid gap-px bg-border sm:grid-cols-2 xl:grid-cols-3">
        {[...byProvider.entries()].map(([name, rows]) => {
          const configured = rows.every((r) => r.configured);
          const requires = [...new Set(rows.flatMap((r) => r.requires))];
          return (
            <div key={name} className="flex items-start gap-3 bg-surface-raised px-5 py-4">
              <BrandIcon name={name} size={32} />
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <span className="text-[13.5px] font-medium">{providerName(name)}</span>
                  {configured ? <StatusBadge state="ok">Ready</StatusBadge> : <StatusBadge state="warn">Needs keys</StatusBadge>}
                </div>
                <div className="mt-0.5 text-[12px] text-muted">{[...new Set(rows.map((r) => r.capability))].join(", ")}</div>
                {!configured && requires.length ? <div className="mt-1 truncate text-[11.5px] text-muted">{requires.join(", ")}</div> : null}
              </div>
            </div>
          );
        })}
        <GridFillers count={byProvider.size} layout="2-3" />
      </div>
    </Section>
  );
}

export default function ModelsPage() {
  const { user } = useSession();
  const { data, error, reload } = useApi<ModelsData>("/models");
  const [switching, setSwitching] = useState<RoleRow | null>(null);

  useEffect(() => {
    document.title = "Models";
  }, []);

  return (
    <div>
      <PageHeader
        title="Models"
        description="Which model does each job. Administrators can change the answer, vision and metadata models here: pick a provider, then a model from the list."
        actions={data?.zdr ? <Badge tone="outline" title="OpenRouter requests only go to providers that do not keep prompts">Zero data retention</Badge> : null}
      />
      <div className="space-y-6 px-6 py-6 lg:px-10">
        {error ? <ErrorNote>{error}</ErrorNote> : null}
        {!data && !error ? <Spinner label="Loading models" /> : null}
        {data ? (
          <>
            {GROUPS.map((group) => (
              <Section key={group.title} title={group.title} description={group.description}>
                {group.roles
                  .map((role) => data.roles.find((r) => r.role === role))
                  .filter((r): r is RoleRow => Boolean(r))
                  .map((row) => (
                    <RoleRowView key={row.role} row={row} isAdmin={user.is_admin} onSwitch={() => setSwitching(row)} onChanged={reload} />
                  ))}
              </Section>
            ))}
            <LocalServer data={data.local_server} />
            {user.is_admin ? <IndexCard /> : null}
            {user.is_admin ? <SwitchHistory /> : null}
            <Providers providers={data.providers} />
            {!user.is_admin ? (
              <p className="flex items-center gap-2 text-[12.5px] text-muted">
                <History size={14} aria-hidden />
                Administrators can test and switch models.
              </p>
            ) : null}
          </>
        ) : null}
      </div>
      {switching ? <SwitchDialog key={switching.role} row={switching} onClose={() => setSwitching(null)} onDone={reload} /> : null}
    </div>
  );
}
