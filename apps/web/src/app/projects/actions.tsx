"use client";

import { useRouter } from "next/navigation";
import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { Project } from "@/lib/api";
import { clientApi } from "@/lib/client";

const primaryBtn =
  "min-h-touch rounded-md bg-accent px-5 font-semibold text-on-accent hover:bg-accent-hover disabled:opacity-60";
const secondaryBtn =
  "min-h-touch rounded-md border border-border-strong bg-transparent px-4 font-semibold text-text";
const reviewBtn =
  "min-h-touch self-start rounded-md border border-status-review-border bg-transparent px-3.5 text-sm font-semibold text-status-review";
const field = "min-h-touch rounded-md border border-border-strong bg-ink px-3 text-text";

/** Native <dialog>: focus trapping, Escape to close and inert background come from the browser. */
function Dialog({
  title,
  trigger,
  children,
}: {
  title: string;
  trigger: (open: () => void) => ReactNode;
  children: (close: () => void) => ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const id = useId();
  const [open, setOpen] = useState(false);
  useEffect(() => {
    const d = ref.current;
    if (!d) return;
    if (open && !d.open) d.showModal();
    if (!open && d.open) d.close();
  }, [open]);
  const close = () => setOpen(false);
  return (
    <>
      {trigger(() => setOpen(true))}
      <dialog
        ref={ref}
        onClose={close}
        aria-labelledby={id}
        className="m-auto w-[min(92vw,480px)] rounded-xl border border-border bg-surface p-6 text-text backdrop:bg-night/80"
      >
        <h2 id={id} className="mt-0 font-display text-xl font-bold">
          {title}
        </h2>
        {children(close)}
      </dialog>
    </>
  );
}

function useSubmit(action: (form: FormData) => Promise<void>) {
  const router = useRouter();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const onSubmit = async (e: FormEvent<HTMLFormElement>, close: () => void) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await action(new FormData(e.currentTarget));
      close();
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Something went wrong");
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, onSubmit };
}

export function NewProjectButton() {
  const { error, busy, onSubmit } = useSubmit(async (f) => {
    await clientApi("/projects", {
      method: "POST",
      body: JSON.stringify({ title: f.get("title"), format: f.get("format") }),
    });
  });
  return (
    <Dialog
      title="New adaptation"
      trigger={(open) => (
        <button type="button" className={primaryBtn} onClick={open}>
          New adaptation
        </button>
      )}
    >
      {(close) => (
        <form className="flex flex-col gap-4" onSubmit={(e) => onSubmit(e, close)}>
          <label className="flex flex-col gap-1.5 text-sm">
            <span className="text-text-secondary">Series title</span>
            <input name="title" required maxLength={500} className={field} />
          </label>
          <fieldset className="m-0 flex flex-col gap-2 border-0 p-0 text-sm">
            <legend className="mb-1.5 text-text-secondary">Format and reading order</legend>
            {[
              ["manga_rtl", "Manga · right to left"],
              ["comic_ltr", "Comic · left to right"],
              ["manhwa_vertical", "Manhwa · vertical scroll"],
            ].map(([v, l], i) => (
              <label key={v} className="flex min-h-touch items-center gap-3">
                <input type="radio" name="format" value={v} defaultChecked={i === 0} className="size-5" />
                {l}
              </label>
            ))}
          </fieldset>
          {error && <p role="alert" className="m-0 text-sm text-status-review">{error}</p>}
          <div className="flex justify-end gap-2.5">
            <button type="button" className={secondaryBtn} onClick={close}>
              Cancel
            </button>
            <button type="submit" className={primaryBtn} disabled={busy}>
              Create project
            </button>
          </div>
        </form>
      )}
    </Dialog>
  );
}

export function RightsButton({ project, isAdmin }: { project: Project; isAdmin: boolean }) {
  const r = project.rights;
  const canEdit = project.role === "owner" || project.role === "editor";
  const submit = useSubmit(async (f) => {
    f.set("allows_cloud_processing", f.get("allows_cloud_processing") ? "true" : "false");
    for (const k of ["licensor", "expires_at"]) if (!f.get(k)) f.delete(k);
    await clientApi(`/projects/${project.id}/rights`, { method: "POST", body: f });
  });
  const router = useRouter();
  const [decideError, setDecideError] = useState<string | null>(null);
  const decide = async (d: "approve" | "reject") => {
    setDecideError(null);
    try {
      await clientApi(`/projects/${project.id}/rights/${d}`, { method: "POST" });
      router.refresh();
    } catch (err) {
      setDecideError(err instanceof Error ? err.message : "Could not update the record");
    }
  };

  return (
    <div className="flex flex-col gap-2">
      {r?.evidence_url && (
        <a href={r.evidence_url} target="_blank" rel="noopener noreferrer" className="text-sm">
          View rights evidence
        </a>
      )}
      {isAdmin && r?.status === "pending" && (
        <div className="flex flex-wrap gap-2">
          <button type="button" className={secondaryBtn} onClick={() => decide("approve")}>
            Approve rights
          </button>
          <button type="button" className={secondaryBtn} onClick={() => decide("reject")}>
            Reject
          </button>
        </div>
      )}
      {decideError && <p role="alert" className="m-0 text-sm text-status-review">{decideError}</p>}
      {canEdit && r?.status !== "approved" && (
        <Dialog
          title={`Rights record · ${project.title}`}
          trigger={(open) => (
            <button type="button" className={reviewBtn} onClick={open}>
              {r ? "Replace rights record" : "Add rights record"}
            </button>
          )}
        >
          {(close) => (
            <form className="flex flex-col gap-4" onSubmit={(e) => submit.onSubmit(e, close)}>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-secondary">Basis</span>
                <select name="basis" required className={field} defaultValue="public_domain">
                  <option value="owned">I own this work</option>
                  <option value="licensed">I have a license</option>
                  <option value="public_domain">Public domain</option>
                </select>
              </label>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-secondary">Justification</span>
                <textarea
                  name="justification"
                  required
                  maxLength={500}
                  rows={3}
                  className={`${field} py-2`}
                  placeholder="e.g. Published 1941, copyright not renewed"
                />
              </label>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-secondary">Licensor (required for licensed works)</span>
                <input name="licensor" maxLength={500} className={field} />
              </label>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-secondary">License expires (optional)</span>
                <input name="expires_at" type="datetime-local" className={field} />
              </label>
              <label className="flex min-h-touch items-center gap-3 text-sm">
                <input type="checkbox" name="allows_cloud_processing" className="size-5" />
                The license allows sending pages to a hosted model provider (Together AI)
              </label>
              <label className="flex flex-col gap-1.5 text-sm">
                <span className="text-text-secondary">Evidence (PDF, PNG, JPEG or WebP)</span>
                <input
                  name="evidence"
                  type="file"
                  required
                  accept="application/pdf,image/png,image/jpeg,image/webp"
                  className="text-sm"
                />
              </label>
              {submit.error && (
                <p role="alert" className="m-0 text-sm text-status-review">{submit.error}</p>
              )}
              <p className="m-0 text-[13px] text-text-muted">
                An admin other than you must approve the record before anything renders.
              </p>
              <div className="flex justify-end gap-2.5">
                <button type="button" className={secondaryBtn} onClick={close}>
                  Cancel
                </button>
                <button type="submit" className={primaryBtn} disabled={submit.busy}>
                  Submit for approval
                </button>
              </div>
            </form>
          )}
        </Dialog>
      )}
    </div>
  );
}
