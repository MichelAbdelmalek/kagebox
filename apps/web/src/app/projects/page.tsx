import { redirect } from "next/navigation";
import { AppShell } from "@/components/AppShell";
import { ApiError, serverApi, type Format, type Me, type Project } from "@/lib/api";
import { NewProjectButton, RightsButton } from "./actions";

const FORMAT_LABEL: Record<Format, string> = {
  manga_rtl: "Manga · RTL",
  comic_ltr: "Comic · LTR",
  manhwa_vertical: "Manhwa · vertical",
};
const BASIS_LABEL = { owned: "Own work", licensed: "Licensed", public_domain: "Public domain" };
// Six segments, as in the design: Ingest, Understand, Bible, Direct, Render+Audio, Validate+Assemble.
const PIPELINE = ["Ingest", "Understand", "Bible", "Direct", "Render & audio", "Validate & assemble"];

function RightsChip({ p }: { p: Project }) {
  const r = p.rights;
  if (r?.status === "approved") {
    return (
      <span className="rounded-pill border border-status-pass-border px-2.5 py-0.5 text-status-pass">
        {BASIS_LABEL[r.basis]} · approved
      </span>
    );
  }
  const label = !r ? "No rights record" : r.status === "pending" ? "Rights pending" : "Rights rejected";
  return (
    <span className="rounded-pill border border-status-review-border px-2.5 py-0.5 text-status-review">
      {label}
    </span>
  );
}

function ProjectCard({ p, isAdmin }: { p: Project; isAdmin: boolean }) {
  const locked = p.rights?.status !== "approved";
  return (
    <article className="flex flex-col overflow-hidden rounded-xl border border-border bg-surface">
      <div aria-hidden="true" className="grid h-[150px] grid-cols-3 grid-rows-2 gap-1.5 bg-paper p-2.5">
        <div className="col-span-2 rounded-[3px] bg-ink" />
        <div className="rounded-[3px] bg-border-strong" />
        <div className="rounded-[3px] bg-border-strong" />
        <div className="col-span-2 rounded-[3px] bg-brand-vermilion" />
      </div>
      <div className="flex flex-col gap-3 p-[18px]">
        <h2 className="m-0 text-lg font-semibold">{p.title}</h2>
        <div className="flex flex-wrap gap-1.5 text-xs">
          <span className="rounded-pill border border-border px-2.5 py-0.5">{FORMAT_LABEL[p.format]}</span>
          <RightsChip p={p} />
        </div>
        <div
          className="grid grid-cols-6 gap-1"
          role="img"
          aria-label={locked ? "Pipeline: locked until rights are approved" : "Pipeline: not started"}
        >
          {PIPELINE.map((s) => (
            <div key={s} className="h-1.5 rounded-[3px] bg-border" />
          ))}
        </div>
        <p className="m-0 text-[13px] text-text-muted">
          {locked
            ? "Locked until a rights record is uploaded and approved by an admin"
            : "Ready for ingest (arrives in Phase 1)"}
        </p>
        <RightsButton project={p} isAdmin={isAdmin} />
      </div>
    </article>
  );
}

export default async function ProjectsPage() {
  let me: Me;
  let projects: Project[];
  try {
    [me, projects] = await Promise.all([serverApi<Me>("/auth/me"), serverApi<Project[]>("/projects")]);
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) redirect("/login");
    throw err;
  }
  return (
    <AppShell current="/projects" profile={process.env.KAGEBOX_PROFILE ?? "cloud"}>
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div className="flex flex-col gap-1.5">
          <h1 className="m-0 font-display text-4xl font-bold tracking-[-0.03em]">Projects</h1>
          <p className="m-0 text-[15px] text-text-muted">
            Each project is one series adapted panel by panel into animated episodes.
          </p>
        </div>
        <NewProjectButton />
      </div>

      <div className="flex flex-wrap items-center gap-3 rounded-lg border border-status-review-border bg-status-review-bg px-[18px] py-3.5 text-sm">
        <span className="font-semibold text-status-review">Rights check</span>
        <span className="flex-[1_1_320px] text-text-secondary">
          Import only works you own, license, or that are public domain. Rendering stays locked until a
          project has an approved rights record.
        </span>
      </div>

      {projects.length === 0 ? (
        <p className="text-text-secondary">No projects yet. Start with “New adaptation”.</p>
      ) : (
        <div className="grid grid-cols-[repeat(auto-fill,minmax(280px,1fr))] gap-5">
          {projects.map((p) => (
            <ProjectCard key={p.id} p={p} isAdmin={me.role === "admin"} />
          ))}
        </div>
      )}
    </AppShell>
  );
}
