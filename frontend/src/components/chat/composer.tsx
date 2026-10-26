"use client";

import clsx from "clsx";
import { ArrowUp, Check, ChevronDown, SlidersHorizontal, Sparkles, Square, X } from "lucide-react";
import { useEffect, useRef, useState, type KeyboardEvent } from "react";

import { ModelIcon, resolveModelDisplay, splitSpec } from "@/components/brand/brand";
import { Input } from "@/components/ui/fields";
import { Popover } from "@/components/ui/overlay";
import { docTypeLabel } from "@/lib/format";
import { api } from "@/lib/api";
import { useApi } from "@/lib/hooks";
import type { Facet, Facets, Filters } from "@/lib/types";

type Key = "project_ids" | "doc_types";

const GROUPS: { key: Key; facet: "project" | "doc_type"; label: string; format: (v: string) => string }[] = [
  { key: "project_ids", facet: "project", label: "Project", format: (v) => v },
  { key: "doc_types", facet: "doc_type", label: "Document type", format: docTypeLabel },
];

export function activeFilterCount(filters: Filters): number {
  return (filters.project_ids?.length ?? 0) + (filters.doc_types?.length ?? 0);
}

function FacetList({ items, selected, format, onToggle }: { items: Facet[]; selected: string[]; format: (v: string) => string; onToggle: (v: string) => void }) {
  if (!items.length) return <p className="px-2 py-1 text-[12.5px] text-muted">Nothing to choose yet.</p>;
  return (
    <ul className="max-h-52 overflow-y-auto">
      {items.map((item) => {
        const on = selected.includes(item.value);
        return (
          <li key={item.value}>
            <button
              type="button"
              onClick={() => onToggle(item.value)}
              aria-pressed={on}
              className="flex w-full items-center gap-2 rounded-md px-2 py-1 text-left text-[13px] hover:bg-surface-hover"
            >
              <span className={clsx("flex h-3.5 w-3.5 shrink-0 items-center justify-center rounded-[4px] border", on ? "border-foreground bg-foreground text-background" : "border-border-strong")}>
                {on ? <Check size={10} strokeWidth={3} aria-hidden /> : null}
              </span>
              <span className="min-w-0 flex-1 truncate">{format(item.value)}</span>
              <span className="text-[11.5px] text-muted tabular">{item.count}</span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function FilterPanel({ filters, onChange }: { filters: Filters; onChange: (f: Filters) => void }) {
  const { data: facets } = useApi<Facets>("/documents/facets");
  const toggle = (key: Key, value: string) => {
    const current = filters[key] ?? [];
    onChange({ ...filters, [key]: current.includes(value) ? current.filter((v) => v !== value) : [...current, value] });
  };
  return (
    <div className="w-[min(560px,calc(100vw-2rem))] p-3">
      <div className="mb-1 px-1">
        <span className="text-[13px] font-semibold">Search only in</span>
        <p className="mt-0.5 text-[12px] text-muted">Filters restrict which documents are searched for this question.</p>
      </div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        {GROUPS.map((group) => (
          <div key={group.key} className="min-w-0">
            <div className="px-2 pb-1 text-[12px] font-medium text-muted">{group.label}</div>
            <FacetList items={facets?.[group.facet] ?? []} selected={filters[group.key] ?? []} format={group.format} onToggle={(v) => toggle(group.key, v)} />
          </div>
        ))}
      </div>
      {activeFilterCount(filters) ? (
        <div className="mt-3 flex justify-end border-t border-border pt-2">
          <button type="button" onClick={() => onChange({})} className="rounded-md px-2 py-1 text-[12.5px] text-muted hover:bg-surface-hover hover:text-foreground">
            Clear filters
          </button>
        </div>
      ) : null}
    </div>
  );
}

type CatalogModel = { id: string; name: string; prompt_per_m: number | null; completion_per_m: number | null; free: boolean; vision?: boolean };
type Catalog = { local: string; shortlist: string[]; current: string; openrouter: CatalogModel[]; openrouter_enabled: boolean };

const MAX_LISTED = 60;

function price(m: CatalogModel): string {
  if (m.free) return "Free";
  if (m.prompt_per_m == null || m.completion_per_m == null) return "";
  return `$${m.prompt_per_m} in / $${m.completion_per_m} out per 1M tokens`;
}

function ModelPicker({ choices, value, onChange }: { choices: string[]; value: string | null; onChange: (v: string | null) => void }) {
  const [open, setOpen] = useState(false);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [loadError, setLoadError] = useState(false);
  const [loading, setLoading] = useState(false);
  const [query, setQuery] = useState("");
  const current = value ? splitSpec(value) : null;
  const catalogNames = catalog?.openrouter ?? null;
  const triggerLabel = value
    ? resolveModelDisplay(value, catalogNames).title
    : "Automatic";

  useEffect(() => {
    if (!open || catalog || loading) return;
    setLoading(true);
    setLoadError(false);
    api<Catalog>("/chat/models")
      .then(setCatalog)
      .catch(() => {
        setCatalog(null);
        setLoadError(true);
      })
      .finally(() => setLoading(false));
  }, [open, catalog, loading]);

  const local = catalog?.local ?? "extractive/default";
  const shortlist = (catalog?.shortlist ?? choices).filter((spec) => spec !== local);
  const needle = query.trim().toLowerCase();
  const matches = (catalog?.openrouter ?? []).filter((m) => !needle || m.id.toLowerCase().includes(needle) || m.name.toLowerCase().includes(needle));

  const option = (spec: string | null, title: string, detail: string) => {
    const parts = spec ? splitSpec(spec) : null;
    const on = spec === value;
    return (
      <button
        key={spec ?? "auto"}
        type="button"
        onClick={() => {
          onChange(spec);
          setOpen(false);
        }}
        className={clsx(
          "flex w-full items-start gap-2.5 rounded-lg px-2.5 py-2 text-left transition-colors duration-150 hover:bg-surface-hover",
          on && "bg-surface-hover",
        )}
      >
        {parts ? <ModelIcon provider={parts.provider} model={parts.model} size={26} /> : <span className="flex h-[26px] w-[26px] shrink-0 items-center justify-center rounded-lg border border-border"><Sparkles size={14} aria-hidden /></span>}
        <span className="min-w-0 flex-1">
          <span className="block truncate text-[13px] font-medium">{title}</span>
          <span className="block truncate text-[12px] text-muted">{detail}</span>
        </span>
        {on ? <Check size={15} className="mt-1 shrink-0" aria-label="Selected" /> : null}
      </button>
    );
  };
  const heading = (text: string) => <div className="px-2.5 pb-1 pt-2.5 text-[12px] font-medium text-muted">{text}</div>;

  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
      side="top"
      align="right"
      className="w-[360px] p-1.5"
      trigger={(props) => (
        <button
          type="button"
          {...props}
          aria-label={`Answer model: ${triggerLabel}. Changes which model writes the answer.`}
          title="Answer model — affects answer generation"
          className="flex h-8 max-w-[220px] items-center gap-1.5 rounded-lg px-2 text-[12.5px] text-muted transition-colors hover:bg-surface-hover hover:text-foreground"
        >
          {current ? <ModelIcon provider={current.provider} model={current.model} size={16} tile={false} /> : <Sparkles size={14} aria-hidden />}
          <span className="min-w-0 truncate">{triggerLabel}</span>
          <ChevronDown size={13} aria-hidden />
        </button>
      )}
    >
      {heading("Answer with")}
      <p className="px-2.5 pb-1 text-[11.5px] text-muted">The selected model affects answer generation, not document search.</p>
      {option(null, "Automatic", "The configured model; the stronger one for multi-part questions")}
      {option(local, "Local (no model)", "Sentences taken from the passages. Free, nothing leaves this server")}
      {shortlist.length ? heading("Shortlist") : null}
      {shortlist.map((spec) => {
        const display = resolveModelDisplay(spec, catalogNames);
        return option(spec, display.title, display.detail);
      })}
      {loading && !catalog ? <p className="px-2.5 py-2 text-[12px] text-muted">Loading models…</p> : null}
      {loadError ? <p className="px-2.5 py-2 text-[12px] text-danger">Could not load the model catalogue.</p> : null}
      {catalog?.openrouter.length ? (
        <>
          {heading(`All OpenRouter models (${catalog.openrouter.length})`)}
          <div className="px-1.5 pb-1">
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search: claude, gpt, llama, gemini, qwen, free …"
              aria-label="Search OpenRouter models"
            />
          </div>
          <div className="max-h-64 overflow-y-auto">
            {matches.slice(0, MAX_LISTED).map((m) =>
              option(
                `openrouter/${m.id}`,
                m.name,
                `${m.vision ? "Text + images" : "Text only"} · ${m.id}${price(m) ? ` · ${price(m)}` : ""}`,
              ),
            )}
            {matches.length > MAX_LISTED ? <p className="px-2.5 py-1.5 text-[12px] text-muted">{matches.length - MAX_LISTED} more: narrow the search.</p> : null}
            {!matches.length ? <p className="px-2.5 py-1.5 text-[12px] text-muted">No model matches.</p> : null}
          </div>
        </>
      ) : catalog && !catalog.openrouter_enabled ? (
        <p className="px-2.5 py-2 text-[12px] text-muted">Set OPENROUTER_API_KEY in .env to answer with OpenRouter models.</p>
      ) : null}
    </Popover>
  );
}

export function Composer({
  onSend,
  onStop,
  streaming,
  filters,
  onFiltersChange,
  model,
  onModelChange,
  modelChoices,
  autoFocus,
  draft,
}: {
  onSend: (question: string) => void;
  onStop: () => void;
  streaming: boolean;
  filters: Filters;
  onFiltersChange: (f: Filters) => void;
  model: string | null;
  onModelChange: (m: string | null) => void;
  modelChoices: string[] | null;
  autoFocus?: boolean;
  draft?: { text: string; nonce: number } | null;
}) {
  const [text, setText] = useState("");
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [appliedDraft, setAppliedDraft] = useState(draft);
  const ref = useRef<HTMLTextAreaElement>(null);

  if (draft !== appliedDraft) {
    setAppliedDraft(draft);
    if (draft) setText(draft.text);
  }
  useEffect(() => {
    const el = ref.current;
    if (!draft || !el) return;
    el.focus();
    el.setSelectionRange(draft.text.length, draft.text.length);
  }, [draft]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    el.style.height = "0px";
    el.style.height = `${Math.min(el.scrollHeight, 220)}px`;
  }, [text]);

  const submit = () => {
    const question = text.trim();
    if (!question || streaming) return;
    onSend(question);
    setText("");
  };
  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const chips: { key: Key; value: string; label: string }[] = [];
  for (const group of GROUPS) for (const v of filters[group.key] ?? []) chips.push({ key: group.key, value: v, label: group.format(v) });
  const remove = (key: Key, value: string) => {
    onFiltersChange({ ...filters, [key]: (filters[key] ?? []).filter((v) => v !== value) });
  };
  const count = activeFilterCount(filters);
  const canSend = Boolean(text.trim()) && !streaming;

  return (
    <div className="rounded-2xl border border-border bg-composer shadow-soft transition-[border-color] duration-150 focus-within:border-border-strong">
      {chips.length ? (
        <div className="flex flex-wrap gap-1.5 px-3 pt-3" aria-label="Active search filters">
          {chips.map((chip) => (
            <span key={`${chip.key}:${chip.value}`} className="inline-flex h-6 items-center gap-1 rounded-md border border-border bg-background pl-2 pr-1 text-[12px]">
              {chip.label}
              <button type="button" onClick={() => remove(chip.key, chip.value)} aria-label={`Remove filter ${chip.label}`} className="rounded p-0.5 text-muted hover:text-foreground">
                <X size={12} />
              </button>
            </span>
          ))}
        </div>
      ) : null}
      <label htmlFor="question" className="sr-only">
        Question
      </label>
      <textarea
        id="question"
        ref={ref}
        dir="auto"
        rows={1}
        value={text}
        autoFocus={autoFocus}
        disabled={streaming}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={onKeyDown}
        placeholder="Ask about drawings, specifications, RFIs, minutes…"
        className="block max-h-[220px] min-h-[52px] w-full resize-none bg-transparent px-3 pb-2 pt-3.5 text-[15px] leading-6 placeholder:text-muted/80 focus:outline-none disabled:opacity-60 sm:px-4"
      />
      <div className="flex flex-wrap items-center gap-1 px-2 pb-2">
        <Popover
          open={filtersOpen}
          onOpenChange={setFiltersOpen}
          side="top"
          trigger={(props) => (
            <button
              type="button"
              {...props}
              aria-label={count ? `Filters, ${count} active` : "Filters"}
              title="Restrict document search"
              className={clsx(
                "flex h-8 items-center gap-1.5 rounded-lg px-2 text-[12.5px] transition-colors duration-150 hover:bg-surface-hover",
                count ? "bg-surface-hover text-foreground" : "text-muted hover:text-foreground",
              )}
            >
              <SlidersHorizontal size={14} aria-hidden />
              Filters{count ? <span className="tabular">({count})</span> : null}
            </button>
          )}
        >
          <FilterPanel filters={filters} onChange={onFiltersChange} />
        </Popover>
        <div className="ml-auto flex items-center gap-1">
          {modelChoices ? <ModelPicker choices={modelChoices} value={model} onChange={onModelChange} /> : null}
          {streaming ? (
            <button
              type="button"
              onClick={onStop}
              aria-label="Stop generating"
              title="Stop — interrupts the active response"
              className="flex h-8 w-8 items-center justify-center rounded-lg border border-border-strong transition-colors duration-150 hover:bg-surface-hover"
            >
              <Square size={12} fill="currentColor" aria-hidden />
            </button>
          ) : (
            <button
              type="button"
              onClick={submit}
              disabled={!canSend}
              aria-label="Send question"
              title="Send (Enter). Shift+Enter for a new line"
              className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-accent-fg transition-opacity duration-150 disabled:opacity-30"
            >
              <ArrowUp size={16} strokeWidth={2.2} aria-hidden />
            </button>
          )}
        </div>
      </div>
      <p className="border-t border-border/70 px-3 py-1.5 text-[11.5px] text-muted sm:px-4">
        Enter send · Shift+Enter new line
        {streaming ? " · Stop interrupts" : ""}
        {count ? " · Filters restrict search" : ""}
        {modelChoices ? " · Model affects the answer" : ""}
      </p>
    </div>
  );
}
