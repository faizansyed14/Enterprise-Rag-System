"use client";

import { FileSearch, ListChecks, MessageSquareQuote } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { CONVERSATIONS_CHANGED, NEW_CHAT } from "@/components/layout/events";
import { ErrorNote, Spinner } from "@/components/ui/feedback";
import { api, errorMessage } from "@/lib/api";
import { useSession } from "@/lib/session";
import { postStream } from "@/lib/sse";
import type { ChatMessage, Citation, Filters, PlanInfo } from "@/lib/types";

import { citationOrder } from "./answer-text";
import { Composer } from "./composer";
import { AssistantMessage, UserMessage } from "./messages";
import { SourcePreviewPane, type SourcePreview } from "./source-preview";

function sourceKey(c: Citation): string {
  return `${c.document_id}:${c.version_id}:${c.page}:${c.n}`;
}

type ServerMessage = {
  id: string;
  role: "user" | "assistant";
  content: string;
  citations: Citation[] | null;
  filters: Record<string, unknown> | null;
  model: string | null;
  status: string | null;
  groundedness: number | null;
  trace_id: string | null;
  created_at: string;
  feedback_score: number | null;
};

let counter = 0;
const uid = () => `m${Date.now().toString(36)}${(counter++).toString(36)}`;

function fromServer(m: ServerMessage): ChatMessage {
  return {
    key: m.id,
    id: m.id,
    role: m.role,
    content: m.content ?? "",
    citations: m.citations ?? [],
    status: m.status ?? undefined,
    model: m.model,
    groundedness: m.groundedness,
    plan: m.role === "assistant" && m.filters && "query_type" in m.filters ? (m.filters as unknown as PlanInfo) : null,
    trace_id: m.trace_id,
    feedback_score: m.feedback_score,
    created_at: m.created_at,
    error: m.role === "assistant" && m.status === "error" && !m.content ? "This answer did not finish." : undefined,
  };
}

function cleanFilters(filters: Filters): Filters {
  const out: Filters = {};
  if (filters.project_ids?.length) out.project_ids = filters.project_ids;
  if (filters.doc_types?.length) out.doc_types = filters.doc_types;
  return out;
}

const SUGGESTIONS = [
  { icon: FileSearch, title: "Find a document", text: "What is the reference number of " },
  { icon: ListChecks, title: "Check a requirement", text: "What does the specification require for " },
  { icon: MessageSquareQuote, title: "Follow up an RFI", text: "What was the response to RFI " },
];

export function ChatView({ conversationId }: { conversationId?: string }) {
  const { app, user } = useSession();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [loading, setLoading] = useState(Boolean(conversationId));
  const [loadError, setLoadError] = useState<string | null>(null);
  const [filters, setFilters] = useState<Filters>({});
  const [model, setModel] = useState<string | null>(null);
  const [draft, setDraft] = useState<{ text: string; nonce: number } | null>(null);
  const [preview, setPreview] = useState<SourcePreview | null>(null);
  const idRef = useRef<string | null>(conversationId ?? null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  const streaming = messages.some((m) => m.streaming);
  const activeSourceKey = preview ? sourceKey(preview.citation) : null;

  const openSource = useCallback((citation: Citation, display: number) => {
    setPreview({ citation, display });
  }, []);

  useEffect(() => {
    if (!conversationId) return;
    let cancelled = false;
    setPreview(null);
    api<{ title: string; filters: Filters | null; messages: ServerMessage[] }>(`/conversations/${conversationId}`)
      .then((data) => {
        if (cancelled) return;
        setMessages(data.messages.map(fromServer));
        setFilters(cleanFilters(data.filters ?? {}));
        document.title = `${data.title} – ${app.name}`;
      })
      .catch((e) => !cancelled && setLoadError(errorMessage(e)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [conversationId, app.name]);

  // "New question" from the sidebar while this page is already open: start over.
  useEffect(() => {
    const reset = () => {
      abortRef.current?.abort();
      idRef.current = null;
      setMessages([]);
      setFilters({});
      setPreview(null);
      setLoadError(null);
      document.title = app.name;
    };
    window.addEventListener(NEW_CHAT, reset);
    return () => window.removeEventListener(NEW_CHAT, reset);
  }, [app.name]);

  // Follow the answer as it streams, unless the reader has scrolled up.
  useEffect(() => {
    const el = scrollRef.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [messages]);
  const onScroll = () => {
    const el = scrollRef.current;
    if (el) stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  useEffect(() => () => abortRef.current?.abort(), []);

  const send = useCallback(
    async (question: string) => {
      if (abortRef.current) return;
      const key = uid();
      setMessages((list) => [
        ...list,
        { key: uid(), role: "user", content: question, citations: [] },
        { key, role: "assistant", content: "", citations: [], streaming: true, stage: "plan", stageText: "Understanding the question", steps: [] },
      ]);
      stick.current = true;
      const controller = new AbortController();
      abortRef.current = controller;
      const update = (fn: (m: ChatMessage) => ChatMessage) => setMessages((list) => list.map((m) => (m.key === key ? fn(m) : m)));
      try {
        await postStream(
          "/chat",
          { question, conversation_id: idRef.current, filters: cleanFilters(filters), model },
          ({ event, data }) => {
            switch (event) {
              case "conversation":
                if (!idRef.current) {
                  idRef.current = data.conversation_id;
                  window.history.replaceState(null, "", `/c/${data.conversation_id}`);
                  window.dispatchEvent(new Event(CONVERSATIONS_CHANGED));
                }
                break;
              case "status":
                update((m) => ({
                  ...m,
                  stage: data.stage,
                  stageText: data.message,
                  model: data.model ?? m.model,
                  steps: data.stage === "research" ? [...(m.steps ?? []), data.message] : m.steps,
                }));
                break;
              case "filters":
                update((m) => ({ ...m, plan: data }));
                break;
              case "delta":
                update((m) => ({ ...m, content: m.content + data.text }));
                break;
              case "citation":
                update((m) => ({ ...m, citations: [...m.citations, data] }));
                break;
              case "done":
                update((m) => {
                  const content = data.answer || m.content;
                  const citations = m.citations;
                  queueMicrotask(() => {
                    if (!citations.length) return;
                    const order = citationOrder(content);
                    const first = [...citations].sort((a, b) => (order.get(a.n) ?? 999) - (order.get(b.n) ?? 999))[0];
                    if (first) setPreview({ citation: first, display: order.get(first.n) ?? first.n });
                  });
                  return {
                    ...m,
                    done: data,
                    content,
                    status: data.status,
                    model: data.model,
                    groundedness: data.groundedness,
                    id: data.message_id,
                    trace_id: data.trace_id,
                    streaming: false,
                  };
                });
                break;
              case "error":
                update((m) => ({ ...m, error: data.message, errorCode: data.code, streaming: false }));
                break;
            }
          },
          controller.signal,
        );
      } catch (e) {
        if (controller.signal.aborted) update((m) => ({ ...m, streaming: false, error: m.content ? undefined : "Stopped." }));
        else update((m) => ({ ...m, streaming: false, error: errorMessage(e) }));
      } finally {
        update((m) => (m.streaming ? { ...m, streaming: false } : m));
        abortRef.current = null;
        window.dispatchEvent(new Event(CONVERSATIONS_CHANGED));
      }
    },
    [filters, model],
  );

  const stop = () => abortRef.current?.abort();
  const choices = app.allow_model_switch ? app.model_choices : null;
  const empty = !loading && !messages.length && !loadError;

  const composer = (
    <Composer
      onSend={send}
      onStop={stop}
      streaming={streaming}
      filters={filters}
      onFiltersChange={setFilters}
      model={model}
      onModelChange={setModel}
      modelChoices={choices}
      autoFocus
      draft={draft}
    />
  );

  return (
    <div className="flex h-full min-h-0">
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        {empty ? (
          <div className="flex min-h-0 flex-1 flex-col items-center justify-center overflow-y-auto px-4 py-10">
            <div className="w-full max-w-[720px]">
              <h1 className="font-display text-[40px] font-medium leading-[1.05] tracking-[-0.01em] sm:text-[46px]">Ask your project documents</h1>
              <p className="mb-7 mt-3 text-[14.5px] text-muted">Every answer names the document and page it comes from.</p>
              {composer}
              <div className="mt-5 grid gap-2 sm:grid-cols-2">
                {SUGGESTIONS.map((s) => (
                  <button
                    key={s.title}
                    type="button"
                    onClick={() => setDraft({ text: s.text, nonce: Date.now() })}
                    className="flex items-start gap-3 rounded-xl border border-border px-3.5 py-3 text-left transition-colors hover:border-border-strong hover:bg-surface-hover"
                  >
                    <s.icon size={16} strokeWidth={1.7} className="mt-0.5 shrink-0 text-muted" aria-hidden />
                    <span className="min-w-0">
                      <span className="block text-[13px] font-medium">{s.title}</span>
                      <span className="block truncate text-[12.5px] text-muted">{s.text.trim()}…</span>
                    </span>
                  </button>
                ))}
              </div>
            </div>
          </div>
        ) : (
          <>
            <div ref={scrollRef} onScroll={onScroll} className="min-h-0 flex-1 overflow-y-auto">
              <div className="mx-auto w-full max-w-[760px] space-y-8 px-4 pb-10 pt-8 sm:px-6">
                {loading ? <Spinner label="Opening the conversation" /> : null}
                {loadError ? <ErrorNote>{loadError}</ErrorNote> : null}
                {messages.map((m, index) =>
                  m.role === "user" ? (
                    <UserMessage key={m.key} message={m} />
                  ) : (
                    <AssistantMessage
                      key={m.key}
                      message={m}
                      isAdmin={user.is_admin}
                      showBadge={app.show_model_badge}
                      activeSourceKey={activeSourceKey}
                      onOpen={(citation, display) => openSource(citation, display)}
                      onRetry={
                        !streaming && index === messages.length - 1 && messages[index - 1]?.role === "user"
                          ? () => send(messages[index - 1].content)
                          : undefined
                      }
                    />
                  ),
                )}
              </div>
            </div>
            <div className="px-4 pb-4 sm:px-6">
              <div className="mx-auto w-full max-w-[760px]">
                {composer}
                <p className="mt-2 text-center text-[11.5px] text-muted">
                  Answers come only from documents you can open. Check the cited page before acting on it.
                </p>
              </div>
            </div>
          </>
        )}
      </div>
      {preview ? <SourcePreviewPane preview={preview} onClose={() => setPreview(null)} /> : null}
    </div>
  );
}
