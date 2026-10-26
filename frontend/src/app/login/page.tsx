"use client";

import { LogIn } from "lucide-react";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState, type FormEvent } from "react";

import { BrandIcon, Wordmark } from "@/components/brand/brand";
import { Button } from "@/components/ui/button";
import { ErrorNote, Spinner } from "@/components/ui/feedback";
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
    <main className="relative flex min-h-full flex-col items-center justify-center overflow-hidden px-6 py-16">
      <div
        aria-hidden
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(ellipse_80%_60%_at_50%_-10%,var(--surface),transparent_55%),linear-gradient(180deg,var(--background),var(--surface)_120%)]"
      />
      <div className="relative w-full max-w-[380px]">
        <div className="mb-9 text-center sm:text-left">
          <Wordmark name={appName || "\u00a0"} />
          <h1 className="sr-only">Sign in</h1>
          <p className="mt-3 text-[15px] leading-relaxed text-muted">
            Answers from your project documents, with the page each one comes from.
          </p>
        </div>

        {error ? (
          <div className="mb-5">
            <ErrorNote>{error}</ErrorNote>
          </div>
        ) : null}

        {passwords ? (
          <form onSubmit={submit} className="space-y-4">
            <Field label="E-mail">
              <Input
                type="email"
                autoComplete="username"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoFocus
                className="h-10"
              />
            </Field>
            <Field label="Password">
              <Input
                type="password"
                autoComplete="current-password"
                required
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="h-10"
              />
            </Field>
            <Button
              type="submit"
              variant="primary"
              className="mt-1 h-10 w-full"
              disabled={busy || !email || !password}
              icon={<LogIn size={15} aria-hidden />}
            >
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
              className="flex h-10 w-full items-center justify-center gap-2.5 rounded-lg border border-border-strong text-sm font-medium transition-colors hover:bg-surface-hover"
            >
              <BrandIcon name="microsoft" size={16} tile={false} />
              Sign in with Microsoft
            </a>
          </>
        ) : null}

        <p className="mt-10 text-center text-[12.5px] leading-relaxed text-muted sm:text-left">
          Access follows the permissions on each source. You only see documents you are allowed to open.
        </p>
      </div>
    </main>
  );
}

export default function LoginPage() {
  return (
    <Suspense
      fallback={
        <main className="flex min-h-full items-center justify-center px-6 py-16">
          <Spinner label="Opening sign-in" />
        </main>
      }
    >
      <SignIn />
    </Suspense>
  );
}
