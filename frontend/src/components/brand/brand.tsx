import clsx from "clsx";
import {
  CircleSlash,
  Cpu,
  Database,
  FlaskConical,
  FolderTree,
  Plug,
  Server,
  Upload,
  type LucideIcon,
} from "lucide-react";

import type { Mark } from "./marks";
import { MARKS } from "./marks.generated";

// Every service and model provider gets the same tile. Vendor marks come from icon packages at build time (see
// scripts/brand-icons.mjs) and are drawn in the current text colour, so they sit inside the monochrome palette in
// both themes. Services without a published mark (our own components, file shares) use a neutral glyph.
const NAMES: Record<string, string> = {
  microsoft: "Microsoft",
  sharepoint: "SharePoint",
  onedrive: "OneDrive",
  smb: "Windows file share",
  filesystem: "Server folder",
  upload: "Uploads",
  openrouter: "OpenRouter",
  openai: "OpenAI",
  anthropic: "Anthropic",
  azure_openai: "Azure OpenAI",
  azure_di: "Azure AI Document Intelligence",
  cohere: "Cohere",
  mistral: "Mistral",
  llamaparse: "LlamaParse",
  local: "Local model server",
  openai_compat: "OpenAI-compatible server",
  hashing: "Offline (testing)",
  lexical: "Offline (testing)",
  builtin: "Built in",
  extractive: "Offline extractive",
  none: "Off",
  claude: "Claude",
  gemini: "Gemini",
  gemma: "Gemma",
  meta: "Llama",
  qwen: "Qwen",
  deepseek: "DeepSeek",
  grok: "Grok",
  huggingface: "Hugging Face",
  qdrant: "Qdrant",
  postgresql: "PostgreSQL",
  prometheus: "Prometheus",
  grafana: "Grafana",
  onnx: "ONNX Runtime",
  minio: "MinIO",
  storage: "Object storage",
};

const GLYPHS: Record<string, LucideIcon> = {
  smb: Server,
  filesystem: FolderTree,
  upload: Upload,
  local: Cpu,
  openai_compat: Plug,
  hashing: FlaskConical,
  lexical: FlaskConical,
  builtin: FlaskConical,
  extractive: FlaskConical,
  none: CircleSlash,
  storage: Database,
};

export function providerName(key: string): string {
  return NAMES[key.toLowerCase()] ?? key;
}

/** The family a chat model belongs to, whichever provider serves it ("openrouter/anthropic/claude-haiku-5.5" → claude). */
export function modelFamily(provider: string, model: string): string | null {
  const id = `${provider}/${model}`.toLowerCase();
  const rules: [RegExp, string][] = [
    [/claude/, "claude"],
    [/(^|\/)(gpt|o\d|chatgpt|text-embedding)|openai\//, "openai"],
    [/gemini/, "gemini"],
    [/gemma/, "gemma"],
    [/llama|meta-llama/, "meta"],
    [/qwen/, "qwen"],
    [/deepseek/, "deepseek"],
    [/grok|x-ai\//, "grok"],
    [/mistral|mixtral|codestral|magistral/, "mistral"],
    [/command|cohere/, "cohere"],
  ];
  for (const [pattern, family] of rules) if (pattern.test(id)) return family;
  return null;
}

function MarkSvg({ mark, size, label }: { mark: Mark; size: number; label: string }) {
  return (
    <svg viewBox={mark.viewBox} width={size} height={size} fill="currentColor" role="img" aria-label={label}>
      {mark.paths.map((p, i) => (
        <path key={i} d={p.d} fillOpacity={p.opacity} fillRule={p.evenOdd ? "evenodd" : undefined} clipRule={p.evenOdd ? "evenodd" : undefined} />
      ))}
    </svg>
  );
}

/** A service or provider mark in a small tile. */
export function BrandIcon({ name, size = 28, className, tile = true }: { name: string; size?: number; className?: string; tile?: boolean }) {
  const key = name.toLowerCase();
  const label = providerName(key);
  const mark = MARKS[key];
  const Glyph = GLYPHS[key] ?? Plug;
  const inner = mark ? (
    <MarkSvg mark={mark} size={Math.round(size * (tile ? 0.54 : 1))} label={label} />
  ) : (
    <Glyph size={Math.round(size * (tile ? 0.52 : 0.9))} strokeWidth={1.6} aria-label={label} />
  );
  if (!tile) {
    return (
      <span className={clsx("inline-flex shrink-0 items-center justify-center", className)} title={label}>
        {inner}
      </span>
    );
  }
  return (
    <span
      className={clsx("inline-flex shrink-0 items-center justify-center rounded-lg border border-border bg-surface-raised text-foreground", className)}
      style={{ width: size, height: size }}
      title={label}
    >
      {inner}
    </span>
  );
}

/** Model mark: the model family when it is recognisable (Claude, GPT, Gemini...), otherwise the provider. */
export function ModelIcon({ provider, model, size = 28, className, tile = true }: { provider: string; model: string; size?: number; className?: string; tile?: boolean }) {
  const family = modelFamily(provider, model);
  return <BrandIcon name={family && MARKS[family] ? family : provider} size={size} className={className} tile={tile} />;
}

/** Split a role line ("openrouter/anthropic/claude-haiku-5.5") into provider and model. */
export function splitSpec(spec: string): { provider: string; model: string } {
  const index = spec.indexOf("/");
  if (index < 0) return { provider: spec, model: "" };
  return { provider: spec.slice(0, index), model: spec.slice(index + 1) };
}

/** Short model name for badges: "openrouter/anthropic/claude-haiku-5.5" → "claude-haiku-5.5". */
export function shortModel(spec: string | null | undefined): string {
  if (!spec) return "";
  const parts = spec.split("/");
  return parts[parts.length - 1] || spec;
}

const OFFLINE = new Set(["extractive", "hashing", "lexical", "builtin"]);

/** How a model is named in badges and tables: the model id, or the provider for the built-in offline ones. */
export function modelLabel(provider: string | null | undefined, model: string | null | undefined): string {
  if (!provider) return model ?? "";
  if (OFFLINE.has(provider)) return providerName(provider);
  return shortModel(`${provider}/${model ?? ""}`);
}

/** Typographic wordmark in the display face. */
export function Wordmark({ name, compact = false }: { name: string; compact?: boolean }) {
  return (
    <span className={clsx("select-none font-display font-semibold leading-none tracking-[0.02em]", compact ? "text-[22px]" : "text-[30px]")}>
      {name}
    </span>
  );
}
