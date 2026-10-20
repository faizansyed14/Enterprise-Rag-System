"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api } from "./api";
import type { AppConfig, User } from "./types";

type Session = {
  user: User;
  app: AppConfig;
  refresh: () => Promise<void>;
  signOut: () => Promise<void>;
};

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<{ user: User; app: AppConfig } | null>(null);

  const refresh = useCallback(async () => {
    const data = await api<{ user: User; app: AppConfig }>("/auth/me");
    setState({ user: data.user, app: data.app });
  }, []);

  const signOut = useCallback(async () => {
    try {
      await api("/auth/logout", { method: "POST" });
    } finally {
      // A full page load on purpose: nothing from the signed-in session stays in memory.
      // eslint-disable-next-line @next/next/no-location-assign-relative-destination
      window.location.href = "/login";
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    api<{ user: User; app: AppConfig }>("/auth/me", { signal: controller.signal })
      .then((data) => setState({ user: data.user, app: data.app }))
      .catch(() => undefined);
    return () => controller.abort();
  }, []);

  const value = useMemo(() => (state ? { ...state, refresh, signOut } : null), [state, refresh, signOut]);
  if (!value) {
    return (
      <div className="flex h-full items-center justify-center text-muted" role="status">
        <span className="pulse-dot mr-2 inline-block h-2 w-2 rounded-full bg-current" />
        Loading your workspace
      </div>
    );
  }
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession outside SessionProvider");
  return session;
}
