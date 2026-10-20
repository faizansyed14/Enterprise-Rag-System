// Thin client for the backend API. Same-origin requests with the session cookie; state-changing requests echo
// the CSRF token from the rag_csrf cookie. A 401 sends the browser to the sign-in page.

export class ApiError extends Error {
  status: number;
  detail: string;
  fields: { field: string; message: string }[];

  constructor(status: number, detail: string, fields: { field: string; message: string }[] = []) {
    super(detail);
    this.status = status;
    this.detail = detail;
    this.fields = fields;
  }
}

export function csrfToken(): string {
  if (typeof document === "undefined") return "";
  const match = document.cookie.split("; ").find((c) => c.startsWith("rag_csrf="));
  return match ? decodeURIComponent(match.slice("rag_csrf=".length)) : "";
}

const UNSAFE = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export function apiHeaders(method: string, json = true): HeadersInit {
  const headers: Record<string, string> = {};
  if (json) headers["Content-Type"] = "application/json";
  if (UNSAFE.has(method)) headers["X-CSRF-Token"] = csrfToken();
  return headers;
}

function redirectToLogin(): void {
  if (typeof window !== "undefined" && !window.location.pathname.startsWith("/login")) {
    const next = encodeURIComponent(window.location.pathname + window.location.search);
    // A full page load on purpose: the expired session's state is dropped with it.
    // eslint-disable-next-line @next/next/no-location-assign-relative-destination
    window.location.href = `/login?next=${next}`;
  }
}

export async function api<T>(
  path: string,
  options: { method?: string; body?: unknown; signal?: AbortSignal; form?: FormData; quiet401?: boolean } = {},
): Promise<T> {
  const method = options.method ?? (options.body !== undefined || options.form ? "POST" : "GET");
  const response = await fetch(`/api/v1${path}`, {
    method,
    credentials: "same-origin",
    headers: apiHeaders(method, !options.form),
    body: options.form ?? (options.body !== undefined ? JSON.stringify(options.body) : undefined),
    signal: options.signal,
  });
  if (response.status === 401 && !options.quiet401) {
    redirectToLogin();
  }
  if (!response.ok) {
    let detail = response.statusText || "Request failed";
    let fields: { field: string; message: string }[] = [];
    try {
      const data = await response.json();
      detail = typeof data.detail === "string" ? data.detail : detail;
      fields = Array.isArray(data.errors) ? data.errors : [];
    } catch {
      /* keep the status text */
    }
    throw new ApiError(response.status, detail, fields);
  }
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.fields.length) return `${error.detail}: ${error.fields.map((f) => `${f.field} ${f.message}`).join("; ")}`;
    return error.detail;
  }
  if (error instanceof Error) return error.message;
  return "Something went wrong";
}
