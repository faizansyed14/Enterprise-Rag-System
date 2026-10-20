export type User = {
  id: string;
  email: string;
  display_name: string;
  is_admin: boolean;
  auth_provider: string;
};

export type AppConfig = {
  name: string;
  show_model_badge: boolean;
  allow_model_switch: boolean;
  auth_mode: string;
  model_choices: string[];
  answer_model: string;
  known_projects: string[];
};

export type Citation = {
  n: number;
  document_id: string;
  version_id: string;
  chunk_id: string;
  doc_number: string;
  title: string;
  page: number;
  page_end: number;
  section_path: string;
  file_name: string;
  project: string;
  quote: string;
  score: number;
  verified: boolean;
  missing_numbers: string[];
};

export type DoneEvent = {
  answer: string;
  status: "ok" | "not_found" | "unverified" | "error" | "refused";
  model: string;
  groundedness: number | null;
  citations: number;
  timings_ms: Record<string, number>;
  trace_id: string | null;
  usage: { input_tokens: number; output_tokens: number; cost_usd: number | null };
  documents: { document_id: string; doc_number: string; title: string; pages: number[]; file_name: string }[];
  message_id?: string;
  conversation_id?: string;
  grounding?: { score: number; judge: string; uncited_claims: number };
  closest_label?: string;
};

export type PlanInfo = {
  query_type: string;
  identifiers: string[];
  language: string;
  hard: boolean;
  reasons: string[];
  router: string;
  project_ids?: string[];
  doc_types?: string[];
  exclude_local_only?: boolean;
};

export type ChatMessage = {
  key: string;
  id?: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[];
  status?: string;
  model?: string | null;
  groundedness?: number | null;
  plan?: PlanInfo | null;
  trace_id?: string | null;
  feedback_score?: number | null;
  done?: DoneEvent;
  streaming?: boolean;
  stage?: string;
  stageText?: string;
  steps?: string[];
  error?: string;
  errorCode?: string;
  created_at?: string;
};

export type Filters = {
  project_ids?: string[];
  doc_types?: string[];
};

export type Facet = { value: string; count: number };
export type Facets = { project: Facet[]; doc_type: Facet[]; unassigned?: number };

export type ConversationSummary = { id: string; title: string; updated_at: string; created_at: string };

export type DocumentRow = {
  id: string;
  doc_number: string | null;
  title: string;
  project_ids: string[];
  doc_type: string | null;
  updated_at: string;
  version_id: string;
  page_count: number;
  language: string | null;
  file_name: string | null;
};

export type VersionInfo = {
  id: string;
  title: string;
  language: string | null;
  page_count: number;
  chunk_count: number;
  format: string;
  file_name: string | null;
  path: string | null;
  extract_method: string | null;
  warnings: string[];
  metadata: Record<string, { value: unknown; confidence: number; source: string }>;
  live_at: string | null;
};

export type DocumentDetail = {
  id: string;
  doc_number: string | null;
  title: string;
  project_ids: string[];
  doc_type: string | null;
  spec_section: string | null;
  family_id: string | null;
  local_only: boolean;
  versions: VersionInfo[];
  references: { target: string; kind: string; page_no: number | null; context: string; target_document_id: string | null }[];
  cited_by: { id: string; doc_number: string | null; title: string }[];
  number_key?: string | null;
};

export type PageLine = { text: string; box?: number[] };

export type PageInfo = {
  page_no: number;
  text: string;
  method: string;
  language: string | null;
  label: string | null;
  width: number | null;
  height: number | null;
  is_drawing: boolean;
  title_block: Record<string, string>;
  tables: { rows: string[][]; header_rows: number }[];
  version_id: string;
  page_count: number;
  format: string;
  has_image: boolean;
  lines?: PageLine[] | null;
};

export type RoleRow = {
  role: string;
  capability: string;
  provider: string;
  model: string;
  dims?: number | null;
  options?: Record<string, string>;
  label: string;
  local: boolean;
  description: string;
  provider_description: string;
  known_provider: boolean;
  missing: string[];
  overridden: boolean;
  env_label: string;
  switchable: boolean;
};
