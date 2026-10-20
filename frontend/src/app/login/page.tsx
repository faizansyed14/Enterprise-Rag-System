"use client";

import { CircleAlert, LogIn } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";

import { BrandIcon, Wordmark } from "@/components/brand/brand";
import { Button } from "@/components/ui/button";
import { Field, Input } from "@/components/ui/fields";
import { api, errorMessage } from "@/lib/api";

type PublicConfig = { app_name: string; auth_mode: string; entra_enabled: boolean };

const ERRORS: Record<string, string> = {
  microsoft: "Microsoft sign-in did not finish. Try again, or use your e-mail and password if you have one.",
  disabled: "This account is switched off. Ask an administrator to turn it back on.",
};

/** Only same-site paths are allowed after sign-in. */
function safeNext(value: string | null): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.startsWith("/login")) return "/";
  return value;
}

/** Zone markers along the frame of a drawing sheet; the sign-in page is laid out as one. */
function SheetFrame() {
  const columns = ["1", "2", "3", "4", "5", "6", "7", "8"];
  const rows = ["A", "B", "C", "D", "E", "F"];
  return (
    <div aria-hidden className="pointer-events-none absolute inset-4 hidden border border-border-strong/70 md:block">
      <div className="absolute inset-x-0 top-0 flex h-5 border-b border-border">
        {columns.map((c) => (
          <span key={c} className="flex flex-1 items-center justify-center border-r border-border text-[10.5px] text-muted last:border-r-0">
            {c}
          </span>
        ))}
      </div>
      <div className="absolute inset-y-0 left-0 flex w-5 flex-col border-r border-border pt-5">
        {rows.map((r) => (
          <span key={r} className="flex flex-1 items-center justify-center border-b border-border text-[10.5px] text-muted last:border-b-0">
            {r}
          </span>
        ))}
      </div>
    </div>
  );
}

function TitleBlock({ appName }: { appName: string }) {
  const today = new Date().toLocaleDateString(undefined, { day: "2-digit", month: "short", year: "numeric" });
  const cell = "border-border px-3 py-1.5";
  const label = "block text-[10.5px] text-muted";
  return (
    <div aria-hidden className="absolute bottom-4 right-4 hidden w-[340px] border-l border-t border-border-strong/70 text-[12px] md:block">
      <div className="grid grid-cols-[1fr_auto]">
        <div className={`${cell} border-b border-r`}>
          <span className={label}>Project</span>
          <span className="font-display text-[17px] font-semibold leading-tight">{appName}</span>
        </div>
        <div className={`${cell} border-b`}>
          <span className={label}>Sheet</span>
          Sign in
        </div>
        <div className={`${cell} border-r`}>
          <span className={label}>Purpose of issue</span>
          For your project documents
        </div>
        <div className={cell}>
          <span className={label}>Date</span>
          <span className="tabular">{today}</span>
        </div>
      </div>
    </div>
  );
}

function SignIn() {
  const router = useRouter();
  const params = useSearchParams();
  const next = safeNext(params.get("next"));
  const [config, setConfig] = useState<PublicConfig | null>(null);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(ERRORS[params.get("error") ?? ""] ?? null);

  useEffect(() => {
    api<PublicConfig>("/auth/config", { quiet401: true }).then(setConfig).catch(() => setConfig({ app_name: "RAG", auth_mode: "local", entra_enabled: false }));
    // Already signed in: go straight on.
    api("/auth/me", { quiet401: true }).then(() => router.replace(next)).catch(() => undefined);
  }, [router, next]);

  useEffect(() => {
    if (config?.app_name) document.title = `Sign in – ${config.app_name}`;
  }, [config]);

  async function submit(event: FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api("/auth/login", { body: { email: email.trim(), password }, quiet401: true });
      router.replace(next);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  const appName = config?.app_name ?? "";
  const passwords = config ? config.auth_mode !== "entra" : true;
  const microsoft = Boolean(config?.entra_enabled);

  return (
    <main className="relative flex min-h-full items-center justify-center px-6 py-16">
      <SheetFrame />
      <TitleBlock appName={appName} />
      <div className="relative w-full max-w-[360px]">
        <div className="mb-10">
          <Wordmark name={appName || " "} />
          <p className="mt-3 text-[14px] leading-relaxed text-muted">Answers from your project documents, with the page each one comes from.</p>
        </div>

        {error ? (
          <div role="alert" className="mb-5 flex items-start gap-2 rounded-lg border border-danger/30 bg-danger-soft px-3 py-2.5 text-[13px] text-danger">
            <CircleAlert size={15} className="mt-0.5 shrink-0" aria-hidden />
            <span>{error}</span>
          </div>
        ) : null}

        {passwords ? (
          <form onSubmit={submit} className="space-y-4">
            <Field label="E-mail">
              <Input type="email" autoComplete="username" required value={email} onChange={(e) => setEmail(e.target.value)} autoFocus />
            </Field>
            <Field label="Password">
              <Input type="password" autoComplete="current-password" required value={password} onChange={(e) => setPassword(e.target.value)} />
            </Field>
            <Button type="submit" variant="primary" className="w-full" disabled={busy || !email || !password} icon={<LogIn size={15} aria-hidden />}>
              {busy ? "Signing in" : "Sign in"}
            </Button>
          </form>
        ) : null}

        {microsoft ? (
          <>
            {passwords ? (
              <div className="my-5 flex items-center gap-3 text-[12px] text-muted">
                <span className="h-px flex-1 bg-border" />
                or
                <span className="h-px flex-1 bg-border" />
              </div>
            ) : null}
            <a
              href="/api/v1/auth/oidc/login"
              className="flex h-9 w-full items-center justify-center gap-2.5 rounded-lg border border-border-strong text-sm font-medium transition-colors hover:bg-surface-hover"
            >
              <BrandIcon name="microsoft" size={16} tile={false} />
              Sign in with Microsoft
            </a>
          </>
        ) : null}

        <p className="mt-8 text-[12.5px] text-muted">Access follows the permissions on each source. You only see documents you are allowed to open.</p>
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense fallback={null}>
      <SignIn />
    </Suspense>
  );
}
