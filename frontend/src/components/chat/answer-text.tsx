"use client";

import { useState, type ReactNode } from "react";

import type { Citation } from "@/lib/types";

// A small, safe renderer for the answer text: paragraphs, headings, lists, tables, bold, italic, inline code and
// citation markers ([2]). No HTML from the model is ever interpreted.

const MARKER = /\[(\d{1,3})\]/g;

/** Display numbers for citations: 1, 2, 3 in the order they first appear in the answer. */
export function citationOrder(text: string): Map<number, number> {
  const order = new Map<number, number>();
  for (const match of text.matchAll(MARKER)) {
    const n = Number(match[1]);
    if (!order.has(n)) order.set(n, order.size + 1);
  }
  return order;
}

type MarkerProps = {
  n: number;
  display: number;
  citation?: Citation;
  onOpen?: (citation: Citation) => void;
};

function Marker({ display, citation, onOpen }: MarkerProps) {
  const [hover, setHover] = useState(false);
  const live = Boolean(citation && onOpen);
  return (
    <span className="relative inline-block" onMouseEnter={() => setHover(true)} onMouseLeave={() => setHover(false)}>
      <button
        type="button"
        disabled={!live}
        onClick={() => citation && onOpen?.(citation)}
        onFocus={() => setHover(true)}
        onBlur={() => setHover(false)}
        aria-label={
          citation
            ? `Source ${display}: ${citation.doc_number || citation.file_name || citation.title}, page ${citation.page}${citation.verified ? ", verified quote" : ", quote not verified"}`
            : `Source ${display}`
        }
        className="mx-[1px] inline-flex h-[18px] min-w-[18px] items-center justify-center rounded-[4px] border border-border-strong bg-surface-raised px-[4px] align-[2px] text-[11px] font-semibold leading-none tabular text-foreground/80 underline-offset-2 transition-colors duration-150 enabled:hover:border-foreground enabled:hover:bg-surface-hover enabled:hover:text-foreground enabled:focus-visible:border-foreground disabled:cursor-default disabled:opacity-70"
      >
        {display}
      </button>
      {hover && citation ? (
        <span
          role="tooltip"
          className="absolute bottom-[calc(100%+6px)] left-1/2 z-20 w-[300px] -translate-x-1/2 rounded-xl border border-border bg-background p-3 text-left shadow-panel"
        >
          <span className="flex items-center gap-2 text-[12px]">
            <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-border text-[11px] font-semibold tabular">
              {display}
            </span>
            <span className="min-w-0 truncate font-semibold">{citation.doc_number || citation.file_name || citation.title}</span>
            <span className="shrink-0 text-muted tabular">p. {citation.page}</span>
          </span>
          <span className="mt-1.5 block text-[11.5px] text-muted">
            {citation.verified ? "Verified on the cited page" : "Quote not matched on the cited page"}
          </span>
          {citation.quote ? (
            <span className="source-text mt-1.5 line-clamp-4 block text-[14.5px] leading-snug text-foreground/90">{citation.quote}</span>
          ) : null}
        </span>
      ) : null}
    </span>
  );
}

type InlineContext = {
  order: Map<number, number>;
  byN: Map<number, Citation>;
  onOpen?: (citation: Citation) => void;
};

function inline(text: string, ctx: InlineContext, keyBase: string): ReactNode[] {
  const out: ReactNode[] = [];
  const pattern = /(\*\*[^*\n]+\*\*|__[^_\n]+__|\*[^*\s][^*\n]*\*|`[^`\n]+`|\[\d{1,3}\])/g;
  let last = 0;
  let i = 0;
  for (const match of text.matchAll(pattern)) {
    const token = match[0];
    const start = match.index ?? 0;
    if (start > last) out.push(text.slice(last, start));
    const key = `${keyBase}-${i++}`;
    if (token.startsWith("**") || token.startsWith("__")) {
      out.push(<strong key={key} className="font-semibold">{inline(token.slice(2, -2), ctx, key)}</strong>);
    } else if (token.startsWith("`")) {
      out.push(<code key={key} className="rounded bg-surface px-1 py-[1px] text-[0.92em]">{token.slice(1, -1)}</code>);
    } else if (token.startsWith("[")) {
      const n = Number(token.slice(1, -1));
      out.push(<Marker key={key} n={n} display={ctx.order.get(n) ?? n} citation={ctx.byN.get(n)} onOpen={ctx.onOpen} />);
    } else {
      out.push(<em key={key}>{inline(token.slice(1, -1), ctx, key)}</em>);
    }
    last = start + token.length;
  }
  if (last < text.length) out.push(text.slice(last));
  return out;
}

type Block =
  | { kind: "p"; text: string }
  | { kind: "h"; text: string }
  | { kind: "ul" | "ol"; items: string[] }
  | { kind: "table"; header: string[]; rows: string[][] };

const BULLET = /^\s*[-*•]\s+/;
const NUMBERED = /^\s*\d{1,3}[.)]\s+/;
const cells = (line: string) =>
  line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim());

function blocks(text: string): Block[] {
  const out: Block[] = [];
  const lines = text.replace(/\r/g, "").split("\n");
  let para: string[] = [];
  const flush = () => {
    if (para.length) out.push({ kind: "p", text: para.join(" ") });
    para = [];
  };
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line.trim()) {
      flush();
      continue;
    }
    if (/^#{1,4}\s+/.test(line)) {
      flush();
      out.push({ kind: "h", text: line.replace(/^#{1,4}\s+/, "") });
      continue;
    }
    if (line.trim().startsWith("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      flush();
      const header = cells(line);
      const rows: string[][] = [];
      i += 2;
      while (i < lines.length && lines[i].trim().startsWith("|")) rows.push(cells(lines[i++]));
      i--;
      out.push({ kind: "table", header, rows });
      continue;
    }
    const kind = BULLET.test(line) ? "ul" : NUMBERED.test(line) ? "ol" : null;
    if (kind) {
      flush();
      const item = line.replace(kind === "ul" ? BULLET : NUMBERED, "");
      const previous = out[out.length - 1];
      if (previous && previous.kind === kind) previous.items.push(item);
      else out.push({ kind, items: [item] });
      continue;
    }
    // A continuation line of a list item stays with the item.
    const previous = out[out.length - 1];
    if (!para.length && previous && (previous.kind === "ul" || previous.kind === "ol") && /^\s{2,}/.test(line)) {
      previous.items[previous.items.length - 1] += ` ${line.trim()}`;
      continue;
    }
    para.push(line.trim());
  }
  flush();
  return out;
}

export function AnswerText({
  text,
  citations,
  onOpen,
  streaming,
}: {
  text: string;
  citations: Citation[];
  onOpen?: (citation: Citation) => void;
  streaming?: boolean;
}) {
  const ctx: InlineContext = { order: citationOrder(text), byN: new Map(citations.map((c) => [c.n, c])), onOpen };
  const parsed = blocks(text);
  return (
    <div dir="auto" className="answer-prose">
      {parsed.map((block, index) => {
        const key = `b${index}`;
        const last = index === parsed.length - 1;
        const caret = streaming && last ? "caret" : undefined;
        switch (block.kind) {
          case "h":
            return (
              <h3 key={key} className={caret}>
                {inline(block.text, ctx, key)}
              </h3>
            );
          case "ul":
          case "ol": {
            const List = block.kind === "ul" ? "ul" : "ol";
            return (
              <List key={key} className={block.kind === "ul" ? "list-disc marker:text-muted" : "list-decimal marker:text-muted"}>
                {block.items.map((item, j) => (
                  <li key={`${key}-${j}`} className={caret && j === block.items.length - 1 ? caret : undefined}>
                    {inline(item, ctx, `${key}-${j}`)}
                  </li>
                ))}
              </List>
            );
          }
          case "table":
            return (
              <div key={key} className="my-3 overflow-x-auto rounded-lg border border-border">
                <table>
                  <thead>
                    <tr>
                      {block.header.map((h, j) => (
                        <th key={j}>{inline(h, ctx, `${key}-h${j}`)}</th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {block.rows.map((row, r) => (
                      <tr key={r}>
                        {row.map((cell, j) => (
                          <td key={j}>{inline(cell, ctx, `${key}-${r}-${j}`)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
          default:
            return (
              <p key={key} className={caret}>
                {inline(block.text, ctx, key)}
              </p>
            );
        }
      })}
      {streaming && !parsed.length ? <p className="caret" /> : null}
    </div>
  );
}

/** The answer as plain text, with citation markers renumbered the way they are shown (for copying). */
export function plainAnswer(text: string, order: Map<number, number>): string {
  return text.replace(MARKER, (_, n) => `[${order.get(Number(n)) ?? n}]`);
}
