// The Details panel's steps of each answer, kept in memory for as long as the tab is open: opening another conversation
// and coming back still shows them, a reload does not (nothing is stored on the server or in the browser).

import type { TraceItem } from "./types";

const byMessage = new Map<string, TraceItem[]>();
const LIMIT = 40; // answers kept; the oldest go first

export function rememberTrace(messageId: string | undefined, items: TraceItem[] | undefined): void {
  if (!messageId || !items?.length) return;
  byMessage.delete(messageId);
  byMessage.set(messageId, items);
  while (byMessage.size > LIMIT) byMessage.delete(byMessage.keys().next().value as string);
}

export function recallTrace(messageId: string | undefined): TraceItem[] | undefined {
  return messageId ? byMessage.get(messageId) : undefined;
}

/** Add an item, or replace the earlier copy of it (a model call is sent when it starts and again when it returns). */
export function upsertTrace(items: TraceItem[] | undefined, item: TraceItem): TraceItem[] {
  const list = items ?? [];
  const index = list.findIndex((existing) => existing.id === item.id);
  if (index < 0) return [...list, item];
  const next = list.slice();
  next[index] = item;
  return next;
}
