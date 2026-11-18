"use client";

import { useRouter } from "next/navigation";
import { useState, type FormEvent } from "react";
import { Logo } from "@/components/Logo";
import { clientApi } from "@/lib/client";

export default function LoginPage() {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const form = new FormData(e.currentTarget);
    setBusy(true);
    setError(null);
    try {
      await clientApi("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email: form.get("email"), password: form.get("password") }),
      });
      router.replace("/projects");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Sign-in failed");
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={onSubmit}
        className="flex w-full max-w-sm flex-col gap-4 rounded-xl border border-border bg-surface p-6"
        aria-labelledby="login-title"
      >
        <Logo />
        <h1 id="login-title" className="m-0 font-display text-2xl font-bold tracking-tight">
          Sign in
        </h1>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-secondary">Email</span>
          <input
            name="email"
            type="email"
            autoComplete="username"
            required
            className="min-h-touch rounded-md border border-border-strong bg-ink px-3 text-text"
          />
        </label>
        <label className="flex flex-col gap-1.5 text-sm">
          <span className="text-text-secondary">Password</span>
          <input
            name="password"
            type="password"
            autoComplete="current-password"
            required
            className="min-h-touch rounded-md border border-border-strong bg-ink px-3 text-text"
          />
        </label>
        {error && (
          <p role="alert" className="m-0 text-sm text-status-review">
            {error}
          </p>
        )}
        <button
          type="submit"
          disabled={busy}
          className="min-h-touch rounded-md bg-accent px-5 font-semibold text-on-accent hover:bg-accent-hover disabled:opacity-60"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
