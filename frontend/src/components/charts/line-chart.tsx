"use client";

import { useEffect, useMemo, useRef, useState } from "react";

export type SeriesPoint = { t: number; v: number | null };

function niceMax(value: number, integer: boolean): number {
  if (value <= 0) return integer ? 2 : 1;
  if (integer && value <= 2) return 2;
  const power = 10 ** Math.floor(Math.log10(value));
  for (const step of integer ? [1, 2, 4, 6, 10] : [1, 2, 2.5, 5, 10]) if (step * power >= value) return step * power;
  return 10 * power;
}

const timeLabel = (t: number, long: boolean) =>
  new Date(t).toLocaleTimeString(undefined, long ? { hour: "2-digit", minute: "2-digit", day: "numeric", month: "short" } : { hour: "2-digit", minute: "2-digit" });

/** One series over time: a 2px line on a recessive grid, with a crosshair and tooltip on hover. Gaps (null) break the
 *  line rather than drawing it through zero. A visually hidden table carries the same numbers for screen readers. */
export function LineChart({
  points,
  label,
  format = (v) => String(Math.round(v)),
  height = 168,
  area = true,
  integer = false,
}: {
  points: SeriesPoint[];
  label: string;
  format?: (v: number) => string;
  height?: number;
  area?: boolean;
  /** Counts: the axis uses whole numbers only. */
  integer?: boolean;
}) {
  const box = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(560);
  const [hover, setHover] = useState<number | null>(null);

  useEffect(() => {
    const el = box.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(Math.max(200, entry.contentRect.width)));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const values = points.map((p) => p.v).filter((v): v is number => v !== null);
  const max = niceMax(Math.max(0, ...values), integer);
  const ticks = [0, max / 2, max];
  // Room for the widest axis label (about 6.4 px per character at 10.5 px).
  const m = { left: 14 + Math.max(...ticks.map((t) => format(t).length)) * 6.4, right: 12, top: 10, bottom: 24 };
  const w = width - m.left - m.right;
  const h = height - m.top - m.bottom;
  const t0 = points[0]?.t ?? 0;
  const t1 = points[points.length - 1]?.t ?? 1;
  const span = Math.max(1, t1 - t0);
  const x = (t: number) => m.left + ((t - t0) / span) * w;
  const y = (v: number) => m.top + h - (v / max) * h;
  const long = span > 20 * 3600 * 1000;

  const { line, fill, dots } = useMemo(() => {
    let d = "";
    let a = "";
    const single: SeriesPoint[] = [];
    let run: SeriesPoint[] = [];
    const flush = () => {
      if (!run.length) return;
      if (run.length === 1) single.push(run[0]);
      d += run.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v as number).toFixed(1)}`).join("");
      a += `M${x(run[0].t).toFixed(1)},${y(0)}` + run.map((p) => `L${x(p.t).toFixed(1)},${y(p.v as number).toFixed(1)}`).join("") + `L${x(run[run.length - 1].t).toFixed(1)},${y(0)}Z`;
      run = [];
    };
    for (const p of points) {
      if (p.v === null) flush();
      else run.push(p);
    }
    flush();
    return { line: d, fill: a, dots: single };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- x and y are derived from the listed values
  }, [points, width, height, max, m.left]);

  const xTicks = points.length > 1 ? [0, 1 / 3, 2 / 3, 1].map((f) => t0 + f * span) : [];
  const hovered = hover !== null ? points[hover] : null;

  const onMove = (event: React.MouseEvent<SVGRectElement>) => {
    if (!points.length) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const t = t0 + ((event.clientX - rect.left) / rect.width) * span;
    let best = 0;
    for (let i = 1; i < points.length; i++) if (Math.abs(points[i].t - t) < Math.abs(points[best].t - t)) best = i;
    setHover(best);
  };

  return (
    <div ref={box} className="relative w-full">
      <svg width={width} height={height} role="img" aria-label={label} className="block text-foreground">
        {ticks.map((tick) => (
          <g key={tick}>
            <line x1={m.left} x2={width - m.right} y1={y(tick)} y2={y(tick)} stroke="var(--border)" strokeDasharray={tick === 0 ? undefined : "2 4"} />
            <text x={m.left - 8} y={y(tick)} dy="0.32em" textAnchor="end" className="fill-muted text-[10.5px] tabular">
              {format(tick)}
            </text>
          </g>
        ))}
        {xTicks.map((t, i) => (
          <text key={t} x={x(t)} y={height - 6} textAnchor={i === 0 ? "start" : i === xTicks.length - 1 ? "end" : "middle"} className="fill-muted text-[10.5px] tabular">
            {timeLabel(t, long)}
          </text>
        ))}
        {area && fill ? <path d={fill} fill="currentColor" opacity={0.07} /> : null}
        {line ? <path d={line} fill="none" stroke="currentColor" strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" /> : null}
        {dots.map((p) => (
          <circle key={p.t} cx={x(p.t)} cy={y(p.v as number)} r={3} fill="currentColor" />
        ))}
        {hovered ? (
          <g>
            <line x1={x(hovered.t)} x2={x(hovered.t)} y1={m.top} y2={m.top + h} stroke="var(--border-strong)" />
            {hovered.v !== null ? <circle cx={x(hovered.t)} cy={y(hovered.v)} r={4.5} fill="currentColor" stroke="var(--surface-raised)" strokeWidth={2} /> : null}
          </g>
        ) : null}
        <rect x={m.left} y={m.top} width={Math.max(0, w)} height={Math.max(0, h)} fill="transparent" onMouseMove={onMove} onMouseLeave={() => setHover(null)} />
      </svg>
      {hovered ? (
        <div
          className="pointer-events-none absolute z-10 rounded-lg border border-border bg-background px-2.5 py-1.5 text-[12px] shadow-panel"
          style={{ left: Math.min(Math.max(x(hovered.t) + 10, 0), width - 150), top: 4 }}
        >
          <div className="text-muted tabular">{timeLabel(hovered.t, long)}</div>
          <div className="font-medium tabular">{hovered.v === null ? "No data" : format(hovered.v)}</div>
        </div>
      ) : null}
      {!values.length ? <div className="pointer-events-none absolute inset-0 flex items-center justify-center text-[12.5px] text-muted">Nothing in this window</div> : null}
      <table className="sr-only">
        <caption>{label}</caption>
        <tbody>
          {points
            .filter((p) => p.v !== null)
            .map((p) => (
              <tr key={p.t}>
                <td>{timeLabel(p.t, true)}</td>
                <td>{format(p.v as number)}</td>
              </tr>
            ))}
        </tbody>
      </table>
    </div>
  );
}

/** Minute buckets from now back over the window, folded into wider buckets for long windows. */
export function bucketize(rows: { minute: string; value: number | null }[], minutes: number, mode: "sum" | "max"): SeriesPoint[] {
  const size = minutes <= 60 ? 1 : minutes <= 360 ? 5 : 15;
  const step = size * 60_000;
  const end = Math.floor(Date.now() / step) * step;
  const start = end - Math.ceil(minutes / size) * step;
  const buckets = new Map<number, number | null>();
  for (let t = start + step; t <= end; t += step) buckets.set(t, mode === "sum" ? 0 : null);
  for (const row of rows) {
    const t = Math.floor(new Date(row.minute).getTime() / step) * step;
    if (!buckets.has(t) || row.value === null) continue;
    const prev = buckets.get(t) ?? null;
    buckets.set(t, mode === "sum" ? (prev ?? 0) + row.value : Math.max(prev ?? 0, row.value));
  }
  return [...buckets.entries()].map(([t, v]) => ({ t, v }));
}
