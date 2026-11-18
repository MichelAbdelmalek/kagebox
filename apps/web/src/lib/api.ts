import { cookies } from "next/headers";

export type Format = "manga_rtl" | "comic_ltr" | "manhwa_vertical";

export type Rights = {
  id: string;
  basis: "owned" | "licensed" | "public_domain";
  status: "pending" | "approved" | "rejected";
  justification: string;
  licensor: string | null;
  allows_cloud_processing: boolean;
  expires_at: string | null;
  evidence_url: string | null;
};

export type Project = {
  id: string;
  title: string;
  format: Format;
  role: "owner" | "editor" | "viewer";
  rights: Rights | null;
};

export type Me = { id: string; email: string; role: "admin" | "member" };

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

/** Server-side call to the API over the internal network, forwarding the user's cookies. */
export async function serverApi<T>(path: string): Promise<T> {
  const base = process.env.KAGEBOX_API_INTERNAL_URL ?? "http://api:8000";
  const jar = await cookies();
  const res = await fetch(`${base}/api${path}`, {
    headers: { cookie: jar.toString(), accept: "application/json" },
    cache: "no-store",
  });
  if (!res.ok) throw new ApiError(res.status, `API ${path} failed`);
  return (await res.json()) as T;
}
