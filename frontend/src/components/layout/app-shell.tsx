"use client";

import clsx from "clsx";
import {
  Activity,
  BookOpen,
  Cpu,
  FlaskRound,
  LibraryBig,
  LogOut,
  Menu,
  MessagesSquare,
  Moon,
  Plus,
  Sun,
  Telescope,
  Trash2,
  Users,
  X,
} from "lucide-react";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { useCallback, useEffect, useState, useSyncExternalStore, type ReactNode } from "react";

import { Wordmark } from "@/components/brand/brand";
import { CONVERSATIONS_CHANGED, NEW_CHAT } from "@/components/layout/events";
import { IconButton } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/feedback";
import { api } from "@/lib/api";
import { relativeTime } from "@/lib/format";
import { useSession } from "@/lib/session";
import { currentTheme, setTheme, subscribeTheme, type Theme } from "@/lib/theme";
import type { ConversationSummary } from "@/lib/types";

const NAV = [
  { href: "/", label: "Ask", icon: MessagesSquare, admin: false },
  { href: "/documents", label: "Documents", icon: BookOpen, admin: false },
  { href: "/models", label: "Models", icon: Cpu, admin: false },
  { href: "/sources", label: "Sources", icon: LibraryBig, admin: true },
  { href: "/monitor", label: "Monitor", icon: Activity, admin: true },
  { href: "/lab", label: "Retrieval lab", icon: Telescope, admin: true },
  { href: "/evaluation", label: "Evaluation", icon: FlaskRound, admin: true },
  { href: "/users", label: "Users", icon: Users, admin: true },
];

function ConversationList({ onNavigate }: { onNavigate: () => void }) {
  const pathname = usePathname();
  const router = useRouter();
  const [items, setItems] = useState<ConversationSummary[]>([]);
  const [confirming, setConfirming] = useState<string | null>(null);

  const load = useCallback(() => {
    api<{ conversations: ConversationSummary[] }>("/conversations?limit=40")
      .then((d) => setItems(d.conversations))
      .catch(() => undefined);
  }, []);

  useEffect(() => {
    load();
    window.addEventListener(CONVERSATIONS_CHANGED, load);
    return () => window.removeEventListener(CONVERSATIONS_CHANGED, load);
  }, [load]);

  const remove = async (id: string) => {
    setConfirming(null);
    try {
      await api(`/conversations/${id}`, { method: "DELETE" });
    } finally {
      load();
    }
    if (window.location.pathname === `/c/${id}`) {
      window.dispatchEvent(new Event(NEW_CHAT));
      router.push("/");
    }
  };

  if (!items.length) {
    return <p className="px-3 py-2 text-[12.5px] text-sidebar-muted">Your conversations will be listed here.</p>;
  }
  return (
    <ul className="space-y-px">
      {items.map((c) => {
        const active = pathname === `/c/${c.id}`;
        if (confirming === c.id) {
          return (
            <li key={c.id} className="rounded-lg bg-sidebar-hover px-3 py-2 text-[12.5px]">
              <div className="truncate text-sidebar-fg">Delete “{c.title}”?</div>
              <div className="mt-1.5 flex gap-1.5">
                <button type="button" onClick={() => remove(c.id)} className="rounded-md bg-danger px-2 py-0.5 font-medium text-white">
                  Delete
                </button>
                <button type="button" onClick={() => setConfirming(null)} className="rounded-md px-2 py-0.5 text-sidebar-muted hover:text-sidebar-fg">
                  Keep
                </button>
              </div>
            </li>
          );
        }
        return (
          <li key={c.id} className="group relative">
            <Link
              href={`/c/${c.id}`}
              onClick={onNavigate}
              className={clsx(
                "block rounded-lg py-1.5 pl-3 pr-8 text-[13px] transition-colors",
                active ? "bg-sidebar-active text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
              )}
              title={`${c.title} (${relativeTime(c.updated_at)})`}
            >
              <span className="block truncate">{c.title}</span>
            </Link>
            <button
              type="button"
              onClick={() => setConfirming(c.id)}
              aria-label={`Delete conversation ${c.title}`}
              className="absolute right-1 top-1/2 hidden -translate-y-1/2 rounded-md p-1 text-sidebar-muted hover:text-sidebar-fg focus:block group-hover:block"
            >
              <Trash2 size={13} />
            </button>
          </li>
        );
      })}
    </ul>
  );
}

function ThemeSwitch() {
  const theme = useSyncExternalStore<Theme>(subscribeTheme, currentTheme, () => "dark");
  const next = theme === "dark" ? "light" : "dark";
  return (
    <IconButton
      label={`Switch to ${next} theme`}
      onClick={() => setTheme(next)}
      className="text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg"
    >
      {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
    </IconButton>
  );
}

const ADMIN_ROUTES = NAV.filter((item) => item.admin).map((item) => item.href);

export function AppShell({ children }: { children: ReactNode }) {
  const { user, app, signOut } = useSession();
  const pathname = usePathname();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [menuPath, setMenuPath] = useState(pathname);
  const chatRoute = pathname === "/" || pathname.startsWith("/c/");

  // The mobile menu closes when the page changes (state adjusted during render, not in an effect).
  if (menuPath !== pathname) {
    setMenuPath(pathname);
    setOpen(false);
  }

  const nav = NAV.filter((item) => !item.admin || user.is_admin);
  const sidebar = (
    <div className="flex h-full flex-col bg-sidebar text-sidebar-fg">
      <div className="flex h-14 items-center justify-between px-4">
        <Link href="/" aria-label={`${app.name} home`}>
          <Wordmark name={app.name} compact />
        </Link>
        <IconButton label="Close menu" onClick={() => setOpen(false)} className="lg:hidden">
          <X size={16} />
        </IconButton>
      </div>
      <div className="px-3">
        <button
          type="button"
          onClick={() => {
            window.dispatchEvent(new Event(NEW_CHAT));
            router.push("/");
          }}
          className="flex h-9 w-full items-center gap-2 rounded-lg border border-sidebar-border px-3 text-[13px] font-medium transition-colors hover:bg-sidebar-hover"
        >
          <Plus size={15} aria-hidden />
          New question
        </button>
      </div>
      <nav className="mt-4 space-y-px px-3" aria-label="Main">
        {nav.map((item) => {
          const active = item.href === "/" ? chatRoute : pathname.startsWith(item.href);
          const Icon = item.icon;
          return (
            <Link
              key={item.href}
              href={item.href}
              onClick={item.href === "/" ? () => window.dispatchEvent(new Event(NEW_CHAT)) : undefined}
              aria-current={active ? "page" : undefined}
              className={clsx(
                "flex h-8 items-center gap-2.5 rounded-lg px-3 text-[13px] transition-colors",
                active ? "bg-sidebar-active font-medium text-sidebar-fg" : "text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg",
              )}
            >
              <Icon size={15} strokeWidth={1.7} aria-hidden />
              {item.label}
            </Link>
          );
        })}
      </nav>
      <div className="mt-5 min-h-0 flex-1 overflow-y-auto px-3 pb-3">
        <div className="px-3 pb-1.5 text-[12px] font-medium text-sidebar-muted">Recent conversations</div>
        <ConversationList onNavigate={() => setOpen(false)} />
      </div>
      <div className="flex items-center gap-2 border-t border-sidebar-border px-3 py-3">
        <div className="min-w-0 flex-1 px-1">
          <div className="truncate text-[13px] font-medium">{user.display_name || user.email}</div>
          <div className="truncate text-[12px] text-sidebar-muted">{user.is_admin ? "Administrator" : user.email}</div>
        </div>
        <ThemeSwitch />
        <IconButton label="Sign out" onClick={signOut} className="text-sidebar-muted hover:bg-sidebar-hover hover:text-sidebar-fg">
          <LogOut size={16} />
        </IconButton>
      </div>
    </div>
  );

  return (
    <div className="flex h-full">
      <aside className="hidden w-[248px] shrink-0 border-r border-sidebar-border lg:block">{sidebar}</aside>
      {open ? (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setOpen(false)} aria-hidden />
          <div className="absolute inset-y-0 left-0 w-[272px] border-r border-sidebar-border shadow-panel">{sidebar}</div>
        </div>
      ) : null}
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="flex h-12 items-center gap-2 border-b border-border px-3 lg:hidden">
          <IconButton label="Open menu" onClick={() => setOpen(true)}>
            <Menu size={18} />
          </IconButton>
          <Wordmark name={app.name} compact />
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto">
          {!user.is_admin && ADMIN_ROUTES.some((href) => pathname.startsWith(href)) ? (
            <EmptyState title="Administrators only">This page manages sources, models and users. Ask an administrator if you need something changed.</EmptyState>
          ) : (
            children
          )}
        </div>
      </main>
    </div>
  );
}
