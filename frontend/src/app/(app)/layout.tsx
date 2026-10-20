"use client";

import { AppShell } from "@/components/layout/app-shell";
import { ToastProvider } from "@/components/ui/toast";
import { SessionProvider } from "@/lib/session";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  return (
    <SessionProvider>
      <ToastProvider>
        <AppShell>{children}</AppShell>
      </ToastProvider>
    </SessionProvider>
  );
}
