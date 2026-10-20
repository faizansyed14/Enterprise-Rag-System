"use client";

import { useCallback, useEffect, useState } from "react";

import { api, errorMessage } from "./api";

type Result<T> = { key: string | null; data: T | null; error: string | null };

/** Fetch JSON from the API, with reload and optional polling (paused while the tab is hidden).
 *  While a new path loads, the previous data stays on screen unless `keep` is false. */
export function useApi<T>(path: string | null, { refreshMs = 0, keep = true }: { refreshMs?: number; keep?: boolean } = {}) {
  const [result, setResult] = useState<Result<T>>({ key: null, data: null, error: null });
  const [tick, setTick] = useState(0);

  useEffect(() => {
    if (!path) return;
    const controller = new AbortController();
    api<T>(path, { signal: controller.signal }).then(
      (data) => setResult({ key: path, data, error: null }),
      (e) => {
        if (!controller.signal.aborted) setResult((r) => ({ key: path, data: r.key === path ? r.data : null, error: errorMessage(e) }));
      },
    );
    return () => controller.abort();
  }, [path, tick]);

  useEffect(() => {
    if (!refreshMs || !path) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) setTick((t) => t + 1);
    }, refreshMs);
    return () => window.clearInterval(timer);
  }, [refreshMs, path]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const setData = useCallback((data: T | null) => setResult((r) => ({ ...r, data })), []);

  const current = result.key === path;
  return {
    data: current || keep ? result.data : null,
    error: current ? result.error : null,
    loading: Boolean(path) && !current,
    reload,
    setData,
  };
}

/** Escape closes, used by drawers and dialogs. */
export function useEscape(onEscape: () => void, active = true) {
  useEffect(() => {
    if (!active) return;
    const handler = (event: KeyboardEvent) => {
      if (event.key === "Escape") onEscape();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onEscape, active]);
}
