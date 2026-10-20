"use client";

import clsx from "clsx";
import { X } from "lucide-react";
import { useEffect, useRef, type ReactNode } from "react";

import { useEscape } from "@/lib/hooks";

import { IconButton } from "./button";

/** Right-hand panel. On wide screens it sits beside the content; on narrow screens it covers it. */
export function Drawer({
  open,
  onClose,
  title,
  subtitle,
  width = "min(560px, 100vw)",
  children,
  actions,
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  subtitle?: ReactNode;
  width?: string;
  children: ReactNode;
  actions?: ReactNode;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEscape(onClose, open);
  useEffect(() => {
    if (open) ref.current?.focus();
  }, [open]);
  if (!open) return null;
  return (
    <>
      <div className="fixed inset-0 z-30 bg-black/30 lg:hidden" onClick={onClose} aria-hidden />
      <aside
        ref={ref}
        tabIndex={-1}
        role="dialog"
        aria-modal="false"
        aria-label={typeof title === "string" ? title : "Details"}
        className="fixed inset-y-0 right-0 z-40 flex flex-col border-l border-border bg-background shadow-panel outline-none"
        style={{ width }}
      >
        <header className="flex items-start gap-3 border-b border-border px-5 py-4">
          <div className="min-w-0 flex-1">
            <div className="truncate text-[15px] font-semibold">{title}</div>
            {subtitle ? <div className="mt-0.5 truncate text-[12.5px] text-muted">{subtitle}</div> : null}
          </div>
          {actions}
          <IconButton label="Close" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </header>
        <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
      </aside>
    </>
  );
}

export function Dialog({
  open,
  onClose,
  title,
  children,
  footer,
  wide,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  footer?: ReactNode;
  wide?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEscape(onClose, open);
  useEffect(() => {
    if (open) ref.current?.querySelector<HTMLElement>("input, select, textarea, button")?.focus();
  }, [open]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 px-4 py-[8vh]" onMouseDown={onClose}>
      <div
        ref={ref}
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className={clsx("w-full rounded-2xl border border-border bg-background shadow-panel", wide ? "max-w-2xl" : "max-w-md")}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-border px-5 py-3.5">
          <h2 className="text-[15px] font-semibold">{title}</h2>
          <IconButton label="Close" onClick={onClose}>
            <X size={16} />
          </IconButton>
        </header>
        <div className="space-y-4 px-5 py-5">{children}</div>
        {footer ? <footer className="flex justify-end gap-2 border-t border-border px-5 py-3">{footer}</footer> : null}
      </div>
    </div>
  );
}

/** A panel anchored to its trigger; closes on outside click and Escape. */
export function Popover({
  trigger,
  children,
  open,
  onOpenChange,
  align = "left",
  side = "bottom",
  className,
}: {
  trigger: (props: { onClick: () => void; "aria-expanded": boolean; "aria-haspopup": "dialog" }) => ReactNode;
  children: ReactNode;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  align?: "left" | "right";
  side?: "top" | "bottom";
  className?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEscape(() => onOpenChange(false), open);
  useEffect(() => {
    if (!open) return;
    const onDown = (event: MouseEvent) => {
      if (ref.current && !ref.current.contains(event.target as Node)) onOpenChange(false);
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [open, onOpenChange]);
  return (
    <div ref={ref} className="relative">
      {trigger({ onClick: () => onOpenChange(!open), "aria-expanded": open, "aria-haspopup": "dialog" })}
      {open ? (
        <div
          role="dialog"
          className={clsx(
            "absolute z-30 rounded-xl border border-border bg-background shadow-panel",
            side === "top" ? "bottom-[calc(100%+8px)]" : "top-[calc(100%+6px)]",
            align === "right" ? "right-0" : "left-0",
            className,
          )}
        >
          {children}
        </div>
      ) : null}
    </div>
  );
}
