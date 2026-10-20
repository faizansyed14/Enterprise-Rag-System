"use client";

import clsx from "clsx";
import { CircleCheck, CircleX } from "lucide-react";
import { createContext, useCallback, useContext, useState, type ReactNode } from "react";

type Toast = { id: number; text: string; tone: "ok" | "error" };
const ToastContext = createContext<(text: string, tone?: "ok" | "error") => void>(() => undefined);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);
  const push = useCallback((text: string, tone: "ok" | "error" = "ok") => {
    const id = Date.now() + Math.random();
    setToasts((t) => [...t, { id, text, tone }]);
    window.setTimeout(() => setToasts((t) => t.filter((x) => x.id !== id)), tone === "error" ? 7000 : 3500);
  }, []);
  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-5 right-5 z-[60] flex w-[min(380px,calc(100vw-2.5rem))] flex-col gap-2" aria-live="polite">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={clsx(
              "pointer-events-auto flex items-start gap-2 rounded-xl border bg-background px-3.5 py-2.5 text-[13px] shadow-panel",
              t.tone === "error" ? "border-danger/40" : "border-border",
            )}
          >
            {t.tone === "error" ? <CircleX size={16} className="mt-0.5 shrink-0 text-danger" /> : <CircleCheck size={16} className="mt-0.5 shrink-0 text-success" />}
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  return useContext(ToastContext);
}
