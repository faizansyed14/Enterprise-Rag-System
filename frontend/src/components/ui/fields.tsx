import clsx from "clsx";
import type { InputHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from "react";

const control =
  "w-full rounded-lg border border-border bg-surface-raised px-3 text-sm placeholder:text-muted/80 focus:border-border-strong focus:outline-none focus-visible:outline-2 focus-visible:outline-accent";

export function Input({ className, ...rest }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={clsx(control, "h-9", className)} {...rest} />;
}

export function Textarea({ className, ...rest }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={clsx(control, "py-2", className)} {...rest} />;
}

export function Select({ className, children, ...rest }: SelectHTMLAttributes<HTMLSelectElement>) {
  return (
    <select className={clsx(control, "h-9 pr-8", className)} {...rest}>
      {children}
    </select>
  );
}

export function Field({ label, hint, children, error }: { label: string; hint?: string; children: ReactNode; error?: string }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-[13px] font-medium">{label}</span>
      {children}
      {error ? <span className="block text-xs text-danger">{error}</span> : hint ? <span className="block text-xs text-muted">{hint}</span> : null}
    </label>
  );
}

export function Switch({ checked, onChange, label, disabled }: { checked: boolean; onChange: (v: boolean) => void; label: string; disabled?: boolean }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={clsx(
        "relative inline-flex h-5 w-9 shrink-0 items-center rounded-full border transition-colors disabled:opacity-40",
        checked ? "border-accent bg-accent" : "border-border-strong bg-surface",
      )}
    >
      <span
        className={clsx(
          "inline-block h-3.5 w-3.5 rounded-full transition-transform",
          checked ? "translate-x-[18px] bg-accent-fg" : "translate-x-[2px] bg-muted",
        )}
      />
    </button>
  );
}

export function Checkbox({ checked, onChange, children }: { checked: boolean; onChange: (v: boolean) => void; children: ReactNode }) {
  return (
    <label className="flex cursor-pointer items-center gap-2 rounded-md px-1.5 py-1 text-[13px] hover:bg-surface-hover">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="h-3.5 w-3.5 accent-[var(--accent)]"
      />
      <span className="min-w-0 flex-1 truncate">{children}</span>
    </label>
  );
}
