"use client";

const CSRF_COOKIE = "__Host-kb_csrf";

function csrfToken(): string {
  const m = document.cookie.split("; ").find((c) => c.startsWith(`${CSRF_COOKIE}=`));
  return m ? decodeURIComponent(m.slice(CSRF_COOKIE.length + 1)) : "";
}

/** Browser call to the API (same origin via Caddy). Sends the CSRF token on every write. */
export async function clientApi<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  headers.set("accept", "application/json");
  if (init.method && init.method !== "GET") headers.set("x-csrf-token", csrfToken());
  if (init.body && !(init.body instanceof FormData)) headers.set("content-type", "application/json");
  const res = await fetch(`/api${path}`, { ...init, headers, credentials: "same-origin" });
  if (!res.ok) {
    let detail = `Request failed (${res.status})`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      /* keep the generic message */
    }
    throw new Error(detail);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}
