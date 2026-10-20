import clsx from "clsx";
import { AlertTriangle, CircleCheck, CircleX, Info } from "lucide-react";
import type { ReactNode } from "react";

type Tone = "neutral" | "success" | "warning" | "danger" | "outline";

const tones: Record<Tone, string> = {
  neutral: "bg-surface text-muted",
  success: "bg-success-soft text-success",
  warning: "bg-warning-soft text-warning",
  danger: "bg-danger-soft text-danger",
  outline: "border border-border-strong text-muted",
};

export function Badge({ tone = "neutral", children, className, title }: { tone?: Tone; children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={clsx("inline-flex h-5 items-center gap-1 whitespace-nowrap rounded-md px-1.5 text-[11.5px] font-medium", tones[tone], className)}>
      {children}
    </span>
  );
}

/** A state shown with an icon and a word, never by colour alone. */
export function StatusBadge({ state, children }: { state: "ok" | "warn" | "fail" | "info"; children: ReactNode }) {
  const map = {
    ok: { tone: "success" as const, icon: <CircleCheck size={12} aria-hidden /> },
    warn: { tone: "warning" as const, icon: <AlertTriangle size={12} aria-hidden /> },
    fail: { tone: "danger" as const, icon: <CircleX size={12} aria-hidden /> },
    info: { tone: "neutral" as const, icon: <Info size={12} aria-hidden /> },
  }[state];
  return (
    <Badge tone={map.tone}>
      {map.icon}
      {children}
    </Badge>
  );
}

export function Spinner({ label = "Loading" }: { label?: string }) {
  return (
    <span role="status" className="inline-flex items-center gap-2 text-muted">
      <span className="pulse-dot inline-block h-1.5 w-1.5 rounded-full bg-current" aria-hidden />
      <span className="text-[13px]">{label}</span>
    </span>
  );
}

export function EmptyState({ title, children, action, icon }: { title: string; children?: ReactNode; action?: ReactNode; icon?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center px-6 py-14 text-center">
      {icon ? <div className="mb-4 text-muted">{icon}</div> : null}
      <h3 className="font-display text-2xl font-medium">{title}</h3>
      {children ? <div className="mt-2 max-w-md text-[13.5px] text-muted">{children}</div> : null}
      {action ? <div className="mt-5">{action}</div> : null}
    </div>
  );
}

export function ErrorNote({ children }: { children: ReactNode }) {
  return (
    <div role="alert" className="flex items-start gap-2 rounded-lg border border-danger/30 bg-danger-soft px-3 py-2 text-[13px] text-danger">
      <CircleX size={15} className="mt-0.5 shrink-0" aria-hidden />
      <div className="min-w-0">{children}</div>
    </div>
  );
}

export function Meter({ value, max = 1, tone = "neutral", label }: { value: number; max?: number; tone?: "neutral" | "warning" | "danger" | "success"; label: string }) {
  const ratio = Math.max(0, Math.min(1, max ? value / max : 0));
  const color = { neutral: "bg-foreground", warning: "bg-warning", danger: "bg-danger", success: "bg-success" }[tone];
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface" role="meter" aria-label={label} aria-valuenow={value} aria-valuemin={0} aria-valuemax={max}>
      <div className={clsx("h-full rounded-full", color)} style={{ width: `${ratio * 100}%` }} />
    </div>
  );
}
