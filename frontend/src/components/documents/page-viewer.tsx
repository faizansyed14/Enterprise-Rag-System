"use client";

import clsx from "clsx";
import { ArrowRightToLine, ExternalLink, Minus, Plus } from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";

import { IconButton } from "@/components/ui/button";
import { ErrorNote, Spinner } from "@/components/ui/feedback";
import { api } from "@/lib/api";
import type { PageInfo } from "@/lib/types";

const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 2.5, 3];
const PREFETCH_MARGIN = "800px 0px";
const SHEET_FORMATS = new Set(["xlsx", "xls", "xlsm", "ods"]);
const OFFICE_HTML_FORMATS = new Set([
  "docx", "doc", "odt", "rtf",
  "xlsx", "xls", "xlsm", "ods",
  "pptx", "ppt", "odp",
  "text", "html", "email", "msg", "xer",
]);
const TEXT_FORMATS = new Set(["text", "html", "email", "msg", "xer"]);
const TABLE_MARKER_RE = /^\[\[TABLE:(\d+)\]\]$/;

function stepZoom(current: number, dir: -1 | 1): number {
  const i = ZOOMS.findIndex((z) => z >= current - 0.001);
  const at = i < 0 ? ZOOMS.indexOf(1) : i;
  return ZOOMS[Math.min(ZOOMS.length - 1, Math.max(0, at + dir))] ?? current;
}

function renderScaleForZoom(zoom: number): number {
  if (zoom >= 2.5) return 3;
  if (zoom >= 1.5) return 2;
  if (zoom >= 1) return 1.5;
  return 1.25;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Where the quote sits in the page text, tolerant of line breaks and punctuation differences. */
export function findQuote(text: string, quote: string | undefined): [number, number] | null {
  if (!quote) return null;
  const words = quote.split(/[\s\p{P}]+/u).filter(Boolean).slice(0, 40);
  for (const size of [words.length, 20, 12, 8, 5]) {
    if (size > words.length || size < 3) continue;
    const pattern = new RegExp(words.slice(0, size).map(escapeRegExp).join("[\\s\\p{P}]+"), "iu");
    const match = pattern.exec(text);
    if (match) {
      const end = Math.min(text.length, match.index + Math.max(match[0].length, quote.length));
      return [match.index, end];
    }
  }
  return null;
}

function useNearViewport(root: RefObject<HTMLElement | null>, enabled = true) {
  const ref = useRef<HTMLDivElement>(null);
  const [near, setNear] = useState(false);
  useEffect(() => {
    if (!enabled || near) return;
    const el = ref.current;
    if (!el) return;
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry?.isIntersecting) setNear(true);
      },
      { root: root.current, rootMargin: PREFETCH_MARGIN, threshold: 0 },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [root, enabled, near]);
  return [ref, near] as const;
}

const pageInfoCache = new Map<string, PageInfo>();
const pageInfoInflight = new Map<string, Promise<PageInfo>>();

function pageInfoKey(documentId: string, versionId: string | null | undefined, pageNo: number) {
  return `${documentId}:${versionId ?? ""}:${pageNo}`;
}

function loadPageInfo(documentId: string, versionId: string | null | undefined, pageNo: number, signal: AbortSignal) {
  const key = pageInfoKey(documentId, versionId, pageNo);
  const cached = pageInfoCache.get(key);
  if (cached) return Promise.resolve(cached);
  let inflight = pageInfoInflight.get(key);
  if (!inflight) {
    const params = new URLSearchParams();
    if (versionId) params.set("version", versionId);
    const qs = params.toString();
    inflight = api<PageInfo>(`/documents/${documentId}/pages/${pageNo}${qs ? `?${qs}` : ""}`).then((data) => {
      pageInfoCache.set(key, data);
      pageInfoInflight.delete(key);
      return data;
    }, (err) => {
      pageInfoInflight.delete(key);
      throw err;
    });
    pageInfoInflight.set(key, inflight);
  }
  return new Promise<PageInfo>((resolve, reject) => {
    const onAbort = () => reject(new DOMException("Aborted", "AbortError"));
    if (signal.aborted) {
      onAbort();
      return;
    }
    signal.addEventListener("abort", onAbort, { once: true });
    inflight!.then(
      (data) => {
        signal.removeEventListener("abort", onAbort);
        resolve(data);
      },
      (err) => {
        signal.removeEventListener("abort", onAbort);
        reject(err);
      },
    );
  });
}

function usePageInfo(documentId: string, versionId: string | null | undefined, pageNo: number, active: boolean) {
  const key = pageInfoKey(documentId, versionId, pageNo);
  const [info, setInfo] = useState<PageInfo | null>(() => pageInfoCache.get(key) ?? null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!active) return;
    const cached = pageInfoCache.get(key);
    if (cached) {
      setInfo(cached);
      setError(null);
      return;
    }
    const controller = new AbortController();
    loadPageInfo(documentId, versionId, pageNo, controller.signal)
      .then((data) => {
        setInfo(data);
        setError(null);
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted && !(err instanceof DOMException && err.name === "AbortError")) {
          setError(err instanceof Error ? err.message : "Could not load page");
        }
      });
    return () => controller.abort();
  }, [active, documentId, versionId, pageNo, key]);
  return { info, error };
}

function Bitmap({
  url,
  alt,
  onLoaded,
}: {
  url: string;
  alt: string;
  onLoaded?: (size: { width: number; height: number }) => void;
}) {
  const [state, setState] = useState<"loading" | "ready" | "failed">("loading");
  return (
    <>
      {state === "failed" ? (
        <div className="absolute inset-0 flex items-center justify-center p-6 text-center text-[13px] text-muted">
          Page image unavailable. Extracted text is on the right.
        </div>
      ) : (
        // eslint-disable-next-line @next/next/no-img-element -- page images come from the API with the session cookie
        <img
          src={url}
          alt={alt}
          className={clsx(
            "absolute inset-0 h-full w-full select-none object-contain transition-opacity duration-300",
            state === "ready" ? "opacity-100" : "opacity-0",
          )}
          onLoad={(event) => {
            setState("ready");
            const img = event.currentTarget;
            onLoaded?.({ width: img.naturalWidth, height: img.naturalHeight });
          }}
          onError={() => setState("failed")}
          draggable={false}
        />
      )}
      {state === "loading" ? (
        <div className="absolute inset-0 flex items-center justify-center">
          <Spinner label="Drawing page" />
        </div>
      ) : null}
    </>
  );
}

/** Readable HTML preview for Office pages (industry pattern: HTML/CSS, not a tiny PNG). Zoom scales type size. */
function OfficeHtmlPreview({ info, zoom }: { info: PageInfo; zoom: number }) {
  const tables = info.tables ?? [];
  const lines = (info.text || "").split(/\r?\n/);
  // a CSV page is a table and nothing else: show it as a grid, like a spreadsheet
  const onlyATable = TEXT_FORMATS.has(info.format) && tables.length > 0 && lines.slice(1).every((l) => !l.trim() || TABLE_MARKER_RE.test(l.trim()));
  const sheet = SHEET_FORMATS.has(info.format) || onlyATable;
  const em = 14 * zoom;
  const pad = Math.max(4, Math.round(6 * zoom));

  if (sheet && tables[0]) {
    const table = tables[0];
    const headerRows = table.header_rows || 0;
    return (
      <div className="paper overflow-auto rounded-sm bg-white shadow-soft ring-1 ring-black/5 dark:ring-white/10">
        {info.label ? (
          <div
            className="sticky top-0 z-10 border-b border-border bg-[#ecece9] px-3 py-2 font-semibold text-foreground"
            style={{ fontSize: `${Math.max(12, 13 * zoom)}px` }}
          >
            {info.label}
          </div>
        ) : null}
        <table className="min-w-full border-collapse" style={{ fontSize: `${em}px` }}>
          <tbody>
            {table.rows.map((row, r) => (
              <tr key={r} className={r < headerRows ? "bg-[#e8eef7] font-semibold" : r % 2 ? "bg-[#f4f6f8]" : "bg-white"}>
                {row.map((cell, c) => (
                  <td
                    key={c}
                    className="max-w-[28em] border border-[#d2d4d8] align-top whitespace-pre-wrap text-foreground"
                    style={{ padding: `${pad}px ${pad + 4}px` }}
                  >
                    {cell}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );
  }

  // Word / slides: document sheet at readable size (zoom grows type + page width).
  const pageWidth = Math.max(320, Math.round(520 * zoom));
  return (
    <div
      className="paper mx-auto min-h-[60vh] rounded-sm bg-white px-8 py-8 shadow-soft ring-1 ring-black/5 dark:ring-white/10 sm:px-10 sm:py-10"
      style={{ width: `min(100%, ${pageWidth}px)`, fontSize: `${Math.max(13, 15 * zoom)}px`, lineHeight: 1.55 }}
    >
      {info.label ? <div className="mb-4 text-[0.85em] text-muted">{info.label}</div> : null}
      <div dir="auto" className="space-y-3 text-foreground">
        {lines.length ? (
          lines.map((line, i) => {
            const m = TABLE_MARKER_RE.exec(line.trim());
            if (m) {
              const table = tables[Number(m[1])];
              if (!table) return null;
              return (
                <div key={i} className="overflow-x-auto rounded-lg border border-border">
                  <table className="w-full border-collapse text-[0.9em]">
                    <tbody>
                      {table.rows.map((row, r) => (
                        <tr key={r} className={r < (table.header_rows || 0) ? "bg-surface font-medium" : undefined}>
                          {row.map((cell, c) => (
                            <td key={c} className="border border-border/70 px-2.5 py-1.5 align-top">
                              {cell}
                            </td>
                          ))}
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              );
            }
            if (!line.trim()) return <div key={i} className="h-2" />;
            if (line.startsWith("#")) {
              const level = line.match(/^#+/)?.[0].length ?? 1;
              const text = line.replace(/^#+\s*/, "");
              return (
                <div key={i} className="font-semibold" style={{ fontSize: `${1.35 - Math.min(level, 3) * 0.12}em` }}>
                  {text}
                </div>
              );
            }
            return (
              <p key={i} className="whitespace-pre-wrap">
                {line}
              </p>
            );
          })
        ) : (
          <p className="text-muted">No text on this page.</p>
        )}
        {tables.length && !lines.some((l) => TABLE_MARKER_RE.test(l.trim()))
          ? tables.map((table, t) => (
              <div key={`t-${t}`} className="overflow-x-auto rounded-lg border border-border">
                <table className="w-full border-collapse text-[0.9em]">
                  <tbody>
                    {table.rows.map((row, r) => (
                      <tr key={r} className={r < (table.header_rows || 0) ? "bg-surface font-medium" : undefined}>
                        {row.map((cell, c) => (
                          <td key={c} className="border border-border/70 px-2.5 py-1.5 align-top">
                            {cell}
                          </td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))
          : null}
      </div>
    </div>
  );
}

function PageExtractBody({ info, quote, emphasize }: { info: PageInfo; quote?: string; emphasize: boolean }) {
  const markRef = useRef<HTMLElement>(null);
  const span = useMemo(() => findQuote(info.text || "", emphasize ? quote : undefined), [info.text, quote, emphasize]);
  useEffect(() => {
    if (span) markRef.current?.scrollIntoView({ block: "center", behavior: "smooth" });
  }, [span]);
  const text = info.text || "";
  return (
    <div className="space-y-5">
      {text ? (
        <div dir="auto" className="source-text whitespace-pre-wrap text-[15px] leading-[1.65] text-foreground/95">
          {span ? (
            <>
              {text.slice(0, span[0])}
              <mark ref={markRef} className="rounded-sm bg-highlight px-0.5 text-foreground">
                {text.slice(span[0], span[1])}
              </mark>
              {text.slice(span[1])}
            </>
          ) : (
            text
          )}
        </div>
      ) : (
        <p className="text-[13px] text-muted">No text was read from this page.</p>
      )}
      {info.tables?.length ? (
        <div className="space-y-3">
          {info.tables.map((table, t) => (
            <div key={t} className="overflow-x-auto rounded-xl border border-border bg-background">
              <table className="w-full border-collapse text-[12.5px]">
                <tbody>
                  {table.rows.map((row, r) => (
                    <tr key={r} className={r < (table.header_rows || 0) ? "bg-surface font-medium" : undefined}>
                      {row.map((cell, c) => (
                        <td key={c} className="border-b border-r border-border/70 px-2.5 py-1 align-top last:border-r-0">
                          {cell}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

function RawPage({
  documentId,
  versionId,
  pageNo,
  zoom,
  scrollRoot,
  quote,
  activePage,
  hasImageHint,
  compact,
  onJumpExtract,
}: {
  documentId: string;
  versionId?: string | null;
  pageNo: number;
  zoom: number;
  scrollRoot: RefObject<HTMLElement | null>;
  quote?: string;
  activePage: number;
  hasImageHint: boolean;
  compact?: boolean;
  onJumpExtract?: (pageNo: number) => void;
}) {
  const [wrapRef, near] = useNearViewport(scrollRoot);
  const { info, error } = usePageInfo(documentId, versionId, pageNo, near);
  const [loadedUrl, setLoadedUrl] = useState<string | null>(null);
  const [naturalRatio, setNaturalRatio] = useState<number | null>(null);
  const [boxes, setBoxes] = useState<number[][]>([]);
  const firstBox = useRef<HTMLSpanElement>(null);
  const fmt = info?.format || "";
  const useHtmlPreview = Boolean(info && OFFICE_HTML_FORMATS.has(fmt));
  const hasImage = info?.has_image ?? hasImageHint;
  const sheetLike = SHEET_FORMATS.has(fmt);
  const ratio =
    naturalRatio ??
    (info?.width && info?.height ? info.width / info.height : sheetLike ? 1.35 : 1 / 1.414);
  const scale = renderScaleForZoom(zoom);
  const url = `/api/v1/documents/${documentId}/pages/${pageNo}/image?${new URLSearchParams({
    ...(versionId ? { version: versionId } : {}),
    scale: String(scale),
  })}`;
  const loaded = loadedUrl === url;
  const wantHighlight = Boolean(quote && quote.length >= 3 && hasImage && !useHtmlPreview && pageNo === activePage && near);

  useEffect(() => {
    setNaturalRatio(null);
    setLoadedUrl(null);
  }, [url]);

  useEffect(() => {
    if (!wantHighlight || !quote) {
      setBoxes([]);
      return;
    }
    const params = new URLSearchParams({ q: quote.slice(0, 900) });
    if (versionId) params.set("version", versionId);
    const controller = new AbortController();
    api<{ boxes: number[][] }>(`/documents/${documentId}/pages/${pageNo}/highlights?${params}`, { signal: controller.signal })
      .then((d) => setBoxes(d.boxes))
      .catch(() => setBoxes([]));
    return () => controller.abort();
  }, [wantHighlight, quote, documentId, versionId, pageNo]);

  useEffect(() => {
    if (loaded && boxes.length) firstBox.current?.scrollIntoView({ block: "center", inline: "center", behavior: "smooth" });
  }, [loaded, boxes]);

  return (
    <article
      ref={wrapRef}
      data-page={pageNo}
      className={clsx(
        "scroll-mt-3",
        compact ? "px-3 py-2" : "px-4 py-3 sm:px-6",
        onJumpExtract && "cursor-pointer rounded-lg transition-colors hover:bg-black/[0.03] dark:hover:bg-white/[0.03]",
      )}
      aria-label={onJumpExtract ? `Original page ${pageNo}. Click to show extract.` : `Original page ${pageNo}`}
      onClick={onJumpExtract ? () => onJumpExtract(pageNo) : undefined}
    >
      {!compact ? (
        <div className="mb-2 flex items-baseline justify-between gap-2">
          <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">Page {pageNo}</span>
          <span className="flex items-center gap-2">
            {info?.label && info.label !== String(pageNo) ? (
              <span className="text-[11px] text-muted">Printed {info.label}</span>
            ) : null}
            {onJumpExtract ? (
              <span className="text-[11px] font-medium text-muted">Show in extract →</span>
            ) : null}
          </span>
        </div>
      ) : null}
      {error ? (
        <ErrorNote>{error}</ErrorNote>
      ) : !info && near ? (
        <div className="py-8">
          <Spinner label="Opening page" />
        </div>
      ) : useHtmlPreview && info ? (
        <OfficeHtmlPreview info={info} zoom={zoom} />
      ) : !hasImage && info ? (
        <div className="rounded-xl border border-dashed border-border bg-background/60 px-4 py-10 text-center text-[13px] text-muted">
          No page preview for this format.
        </div>
      ) : (
        <div
          className="relative mx-auto origin-top overflow-hidden rounded-sm bg-white shadow-soft ring-1 ring-black/5 transition-[width] duration-200 dark:ring-white/10"
          style={{ width: `${Math.max(40, zoom * 100)}%`, aspectRatio: `${ratio}` }}
        >
          {near ? (
            <Bitmap
              key={url}
              url={url}
              alt={`Page ${pageNo}`}
              onLoaded={(size) => {
                setLoadedUrl(url);
                if (size.width > 0 && size.height > 0) setNaturalRatio(size.width / size.height);
              }}
            />
          ) : null}
          {loaded
            ? boxes.map((b, i) => (
                <span
                  key={i}
                  ref={i === 0 ? firstBox : undefined}
                  className="pointer-events-none absolute rounded-[2px] bg-[rgba(255,196,0,0.38)] mix-blend-multiply"
                  style={{
                    left: `${b[0] * 100}%`,
                    top: `${b[1] * 100}%`,
                    width: `${(b[2] - b[0]) * 100}%`,
                    height: `${(b[3] - b[1]) * 100}%`,
                  }}
                />
              ))
            : null}
        </div>
      )}
    </article>
  );
}

function ExtractPage({
  documentId,
  versionId,
  pageNo,
  scrollRoot,
  quote,
  activePage,
}: {
  documentId: string;
  versionId?: string | null;
  pageNo: number;
  scrollRoot: RefObject<HTMLElement | null>;
  quote?: string;
  activePage: number;
}) {
  const [wrapRef, near] = useNearViewport(scrollRoot);
  const { info, error } = usePageInfo(documentId, versionId, pageNo, near);
  const active = pageNo === activePage;

  return (
    <article
      ref={wrapRef}
      data-page={pageNo}
      className={clsx(
        "scroll-mt-3 border-b border-border/80 px-5 py-5 transition-colors duration-300 last:border-b-0 sm:px-6",
        active ? "bg-surface-raised" : "bg-transparent",
      )}
      aria-label={`Extracted page ${pageNo}`}
    >
      <div className="mb-3 flex items-baseline justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-[0.08em] text-muted">Page {pageNo}</span>
        {info?.method ? <span className="truncate text-[11px] text-muted">{info.method}</span> : null}
      </div>
      {!near ? (
        <div className="h-24 rounded-lg bg-surface/80" aria-hidden />
      ) : error ? (
        <ErrorNote>{error}</ErrorNote>
      ) : !info ? (
        <div className="py-6">
          <Spinner label="Reading page" />
        </div>
      ) : (
        <PageExtractBody info={info} quote={quote} emphasize={active} />
      )}
    </article>
  );
}

function mostVisiblePage(root: HTMLElement): number | null {
  const pages = root.querySelectorAll<HTMLElement>("[data-page]");
  if (!pages.length) return null;
  const rootRect = root.getBoundingClientRect();
  const mid = rootRect.top + rootRect.height * 0.35;
  let best: { page: number; dist: number } | null = null;
  for (const el of pages) {
    const rect = el.getBoundingClientRect();
    if (rect.bottom < rootRect.top || rect.top > rootRect.bottom) continue;
    const center = rect.top + rect.height * 0.2;
    const dist = Math.abs(center - mid);
    const page = Number(el.dataset.page);
    if (!best || dist < best.dist) best = { page, dist };
  }
  return best?.page ?? null;
}

function scrollPaneToPage(root: HTMLElement, page: number, behavior: ScrollBehavior) {
  const target = root.querySelector<HTMLElement>(`[data-page="${page}"]`);
  if (!target) return;
  const top = target.getBoundingClientRect().top - root.getBoundingClientRect().top + root.scrollTop;
  root.scrollTo({ top: Math.max(0, top - 8), behavior });
}

/** Continuous document reader. `split` = original left + extract right; `raw` = zoomable original only. */
export function PageViewer({
  documentId,
  versionId,
  pageCount,
  page,
  quote,
  onPageChange,
  format,
  toolbarExtra,
  originalHref,
  mode = "split",
}: {
  documentId: string;
  versionId?: string | null;
  pageCount: number;
  page: number;
  quote?: string;
  onPageChange?: (page: number) => void;
  format?: string;
  toolbarExtra?: ReactNode;
  originalHref?: string;
  mode?: "split" | "raw";
}) {
  const rawOnly = mode === "raw";
  const count = Math.max(1, pageCount || 1);
  const pages = useMemo(() => Array.from({ length: count }, (_, i) => i + 1), [count]);
  const hasImageHint = Boolean(
    format && ["pdf", "image", "docx", "doc", "odt", "rtf", "xlsx", "xls", "xlsm", "ods", "pptx", "ppt", "odp", "text", "html", "email", "msg", "xer"].includes(format),
  );
  const officeFmt = Boolean(format && OFFICE_HTML_FORMATS.has(format));
  const [zoom, setZoom] = useState(() => (officeFmt ? 1.25 : 1));
  const [rawPage, setRawPage] = useState(page);
  const [extractPage, setExtractPage] = useState(page);

  useEffect(() => {
    setZoom(officeFmt ? 1.25 : 1);
  }, [documentId, versionId, officeFmt]);
  const rootRef = useRef<HTMLDivElement>(null);
  const leftRef = useRef<HTMLDivElement>(null);
  const rightRef = useRef<HTMLDivElement>(null);
  const jumped = useRef(false);

  // Ctrl/⌘ + scroll zooms the preview, not the browser page.
  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;
    const onWheel = (event: WheelEvent) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      event.preventDefault();
      event.stopPropagation();
      const dir: -1 | 1 = event.deltaY > 0 ? -1 : 1;
      setZoom((z) => stepZoom(z, dir));
    };
    root.addEventListener("wheel", onWheel, { passive: false });
    return () => root.removeEventListener("wheel", onWheel);
  }, []);

  useEffect(() => {
    setRawPage(page);
    setExtractPage(page);
  }, [page, versionId]);

  useEffect(() => {
    jumped.current = false;
  }, [documentId, versionId, quote]);

  // Initial open: both panes start on the requested page; after that they scroll independently.
  useEffect(() => {
    if (jumped.current) return;
    const left = leftRef.current;
    if (!left) return;
    const right = rightRef.current;
    const id = window.requestAnimationFrame(() => {
      scrollPaneToPage(left, page, "auto");
      if (!rawOnly && right) scrollPaneToPage(right, page, "auto");
      jumped.current = true;
    });
    return () => window.cancelAnimationFrame(id);
  }, [page, count, documentId, versionId, rawOnly]);

  const reportPage = useCallback(
    (next: number) => {
      setRawPage(next);
      if (onPageChange && next !== page) onPageChange(next);
    },
    [onPageChange, page],
  );

  const jumpExtractTo = useCallback((pageNo: number) => {
    const right = rightRef.current;
    if (!right) return;
    scrollPaneToPage(right, pageNo, "smooth");
    setExtractPage(pageNo);
  }, []);

  const onLeftScroll = useCallback(() => {
    const left = leftRef.current;
    if (!left) return;
    const next = mostVisiblePage(left);
    if (next == null || next === rawPage) return;
    reportPage(next);
  }, [rawPage, reportPage]);

  const onRightScroll = useCallback(() => {
    const right = rightRef.current;
    if (!right) return;
    const next = mostVisiblePage(right);
    if (next == null || next === extractPage) return;
    setExtractPage(next);
  }, [extractPage]);

  return (
    <div ref={rootRef} className="flex h-full min-h-0 flex-col">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2 sm:px-4">
        <span className="text-[12.5px] text-muted tabular">
          {rawOnly ? (
            <>
              Page <span className="font-medium text-foreground">{rawPage}</span>
              <span className="text-muted"> of {count}</span>
            </>
          ) : (
            <>
              Raw <span className="font-medium text-foreground">{rawPage}</span>
              <span className="text-muted"> · Extract </span>
              <span className="font-medium text-foreground">{extractPage}</span>
              <span className="text-muted"> / {count}</span>
            </>
          )}
        </span>
        {!rawOnly ? (
          <button
            type="button"
            onClick={() => jumpExtractTo(rawPage)}
            className="inline-flex h-8 items-center gap-1.5 rounded-lg border border-border px-2.5 text-[12.5px] font-medium text-muted hover:bg-surface-hover hover:text-foreground"
            title={`Scroll extract to page ${rawPage}`}
          >
            <ArrowRightToLine size={14} aria-hidden />
            Go to page {rawPage}
          </button>
        ) : null}
        <div className="ml-auto flex items-center gap-1">
          <span className="mr-0.5 text-[12px] text-muted tabular" title="Ctrl or ⌘ + scroll to zoom">
            {Math.round(zoom * 100)}%
          </span>
          <IconButton label="Zoom out" onClick={() => setZoom((z) => stepZoom(z, -1))} disabled={zoom <= ZOOMS[0]}>
            <Minus size={15} />
          </IconButton>
          <IconButton
            label="Zoom in"
            onClick={() => setZoom((z) => stepZoom(z, 1))}
            disabled={zoom >= ZOOMS[ZOOMS.length - 1]}
          >
            <Plus size={15} />
          </IconButton>
          {!rawOnly && originalHref ? (
            <a
              href={originalHref}
              target="_blank"
              rel="noopener noreferrer"
              className="ml-1 inline-flex h-8 items-center gap-1.5 rounded-lg px-2.5 text-[12.5px] font-medium text-muted hover:bg-surface-hover hover:text-foreground"
            >
              <ExternalLink size={14} aria-hidden />
              Open file
            </a>
          ) : null}
          {toolbarExtra}
        </div>
      </div>

      <div
        className={clsx(
          "min-h-0 flex-1 overflow-hidden",
          rawOnly ? "flex" : "grid grid-cols-1 divide-y divide-border lg:grid-cols-2 lg:divide-x lg:divide-y-0",
        )}
      >
        <section
          className={clsx(
            "flex min-h-0 min-w-0 flex-col bg-[color-mix(in_srgb,var(--surface)_88%,#c8c4bc_12%)] dark:bg-surface",
            rawOnly ? "h-full w-full" : "max-lg:h-[48%] lg:h-auto",
          )}
          aria-label="Original document"
        >
          {!rawOnly ? (
            <div className="flex items-center justify-between border-b border-border/70 px-4 py-1.5 lg:hidden">
              <span className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">Original</span>
            </div>
          ) : null}
          <div ref={leftRef} onScroll={onLeftScroll} className="min-h-0 flex-1 overflow-auto overscroll-contain py-2">
            {pages.map((n) => (
              <RawPage
                key={n}
                documentId={documentId}
                versionId={versionId}
                pageNo={n}
                zoom={zoom}
                scrollRoot={leftRef}
                quote={quote}
                activePage={rawPage}
                hasImageHint={hasImageHint}
                compact={rawOnly}
                onJumpExtract={rawOnly ? undefined : jumpExtractTo}
              />
            ))}
          </div>
        </section>

        {!rawOnly ? (
          <section className="flex min-h-0 min-w-0 flex-col bg-background max-lg:h-[52%] lg:h-auto" aria-label="Extracted text">
            <div className="flex items-center justify-between border-b border-border/70 px-4 py-1.5 lg:hidden">
              <span className="text-[11px] font-semibold uppercase tracking-[0.1em] text-muted">Extract</span>
            </div>
            <div ref={rightRef} onScroll={onRightScroll} className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
              {pages.map((n) => (
                <ExtractPage
                  key={n}
                  documentId={documentId}
                  versionId={versionId}
                  pageNo={n}
                  scrollRoot={rightRef}
                  quote={quote}
                  activePage={extractPage}
                />
              ))}
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
