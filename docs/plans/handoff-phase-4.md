# Kagebox hand-off: Phase 4, production

Part of the hand-off; start at [`handoff-phase-1-continuation.md`](handoff-phase-1-continuation.md). A **proposal**; Phase 4 work is CPU-only and parts of it (4.5 security hardening) may be pulled earlier, before the stack is ever shared with another person.

## Contents

- [4.1 Episode assembly](#41-episode-assembly)
- [4.2 OpenTimelineIO export for DaVinci Resolve](#42-opentimelineio-export-for-davinci-resolve)
- [4.3 Performance dashboard](#43-performance-dashboard)
- [4.4 Cost dashboard](#44-cost-dashboard)
- [4.5 Production hardening](#45-production-hardening-sec)
- [4.6 Backups and operations](#46-backups-and-operations)
- [4.7 Release](#47-release)
- [Cross-cutting steps and owner decisions](#cross-cutting-steps-and-owner-decisions)

---

## 4.1 Episode assembly

**Goal.** An episode spans one or more chapters (or part of one): ordered chapter exports, opening/ending cards, continuous score cues across chapter boundaries, one loudness pass, one provenance bundle.

**Files:** `pipeline/assemble/episode.py`, migration `00xx_episodes.py` (`episodes`: id, project_id, title, chapter ranges, version; `episode_exports`), API `POST /api/projects/{id}/episodes`, `POST /api/episodes/{id}/assemble`.

**Tasks:**

1. **4.1.1** Episode model and API (editor; audited). Tests: `test_episode_chapters_from_same_project_only`, `test_episode_needs_every_chapter_validated` (no `review` verdicts open, or an explicit owner override recorded).
2. **4.1.2** Assembly reuses 1.8 (timeline, mix, mux) across chapters; the bundle merges chapter bundles and de-duplicates models and attributions. Tests: `test_episode_bundle_covers_all_chapters`, `test_single_loudness_pass`, `test_credit_gate_applies_per_source_work`.

**Estimates:** 4 sessions.

## 4.2 OpenTimelineIO export for DaVinci Resolve

**Dependency:** `opentimelineio` (licence expected Apache-2.0, **verify**; new dependency: **approval**).

**Tasks:**

1. **4.2.1** `pipeline/assemble/otio_export.py`: one timeline with video track (one clip per shot, source = rendered shot file), dialogue track (one clip per voice line), music and foley tracks, markers for review items and hit points; clip metadata carries shot id, panel ids and provenance ref. Tests: `test_otio_round_trip_preserves_clip_count_and_durations`, `test_clip_metadata_has_panel_ids`, `test_markers_for_open_review_items`.
2. **4.2.2** Export package: `.otio` plus media files in a zip with relative paths (built with our own writer; names are generated ids, never user strings). Tests: `test_package_paths_relative_and_safe`, `test_package_includes_bundle_and_credits`.
3. **4.2.3** Manual check in DaVinci Resolve by the owner (import the `.otio`, relink media): record the Resolve version and result in `docs/plans/phase-4-results.md`. Note: Resolve's OTIO import support and supported schema versions are **unverified**: confirm during this task.

**Estimates:** 4 sessions.

## 4.3 Performance dashboard

**Goal.** Seconds per item per stage, wall time, peak memory, cache hit rate, trial-vs-actual projection error, per chapter and over time.

**Tasks:**

1. **4.3.1** Metrics source: stage runs already store `counts`; add a `stage_metrics` table written at finish (stage, chapter, items, cached, failed, seconds per item p50/p95, wall, peak RSS sampled by the worker, load average at start). Tests: `test_metrics_written_on_finish`, `test_no_content_in_metrics` (numbers and ids only).
2. **4.3.2** API `GET /api/projects/{id}/metrics` (viewer). Test `test_metrics_member_only`.
3. **4.3.3** UI page with accessible tables first and charts second (charts follow the design tokens; every chart has a table equivalent). Vitest + Playwright + axe.

**Estimates:** 5 sessions.

## 4.4 Cost dashboard

**Goal.** GPU minutes and money per stage, chapter and episode (rented GPU and any paid service), against budgets from 2.6.

**Tasks:**

1. **4.4.1** Cost model in `configs/costs.yaml` (price per GPU hour per host type, entered by the owner; no external billing API without approval). Tests: `test_cost_from_measured_gpu_seconds`, `test_budget_remaining`.
2. **4.4.2** API and UI as in 4.3; alerts as review items when a chapter exceeds 80 % of its budget. Test `test_budget_warning_review_item`.

**Estimates:** 3 sessions.

## 4.5 Production hardening [SEC]

**Tasks:**

1. **4.5.1 PDF render isolation** (follow-up from 1.1): render PDFs in a dedicated one-shot container (or a `bwrap`/`nsjail` sandbox with `--unshare-net`) on an internal-only network, own uid, **no secrets**, minimal read-only bind set; output pages returned through a pipe or a scratch volume. Tests: `test_pdf_renderer_has_no_secrets_and_no_network` (`test_compose.py`), `test_ingest_works_with_isolated_renderer` (integration).
2. **4.5.2 Cancel route:** owner-only `POST /api/chapters/{id}/{stage}/cancel` that sets the owning flow run to Cancelling through `start_deployment`'s client; audited. Tests: `test_cancel_owner_only`, `test_cancel_marks_stage_interrupted`.
3. **4.5.3 Periodic orphan-run reconciliation** (index section 8, item 2) if not done earlier.
4. **4.5.4 Secrets rotation:** documented and scripted rotation for DB password, S3 keys, session secret, media signing key, Qdrant key, consent key (re-encrypt consent documents with the new `key_id`). Tests: `test_consent_reencryption_preserves_plaintext_hash`.
5. **4.5.5 Multi-user review:** rate limits on login and write routes; session expiry review; CSP report-only collection disabled unless an internal endpoint exists.
6. **4.5.6 Threat model** document `docs/security/threat-model.md` covering uploads, models, ComfyUI, consent, rights, signed media, and the residual risks recorded in Phase 1.

**Estimates:** 8 sessions; independent security review of every task.

## 4.6 Backups and operations

1. **4.6.1** Backups: Postgres logical dumps (`pg_dump` in a one-shot container), object store bucket copies, Qdrant snapshots (the `qdrant-snapshots` volume exists), encrypted at rest with a key from a secret; restore script tested. Tests: `test_backup_job_has_no_egress` and a scripted restore drill on a scratch stack.
2. **4.6.2** Health and logs: structured logs (no content), log retention, `make logs` filters; disk-space alarm for the Docker VM (the November 2026 incident).
3. **4.6.3** Runbook `docs/operations.md`: restore without rebuild, traps from index section 4.5, incident steps.

**Estimates:** 4 sessions.

## 4.7 Release

1. **4.7.1** Release checklist (below) run on a tagged commit.
2. **4.7.2** Versioning: tag `vX.Y.Z`, changelog from Conventional Commits.
3. **4.7.3** Image distribution decision (owner): distributing images with Debian's GPL FFmpeg build has obligations (index: audit consequence 7); options: do not distribute images, ship an LGPL FFmpeg build, or comply with GPL source offers.
4. **4.7.4** Publication duties per source work: credits and the rights holder's reporting condition for the sample.

**Release checklist:**

- [ ] `make test`, `make lint`, `make scan`, `make scan-images` clean; CI green on the tag.
- [ ] `LICENSES.md` complete (models, images, dependencies, custom nodes, LoRAs, audio assets).
- [ ] Every export has a provenance bundle and credits; CC-BY attributions present.
- [ ] No open `review` items on released episodes (or owner overrides recorded).
- [ ] Voices: every used voice is designed or has signed, unrevoked consent; watermarks verified after mux.
- [ ] Music: V7 decision applied (generated, composer or licensed library) and similarity checks passed where applicable.
- [ ] Backups taken and a restore drill done.
- [ ] Threat model reviewed; secrets rotated if the stack was shared.
- [ ] README and runbook up to date.

## Cross-cutting steps and owner decisions

- **CI:** add any new image or dependency to scans; OTIO dependency audited.
- **Licences/provenance:** OTIO exports reference provenance; episode bundles merge chapter bundles.
- **Docker hardening:** renderer and backup jobs without secrets or egress beyond need.
- **Accessibility:** dashboards and episode screens pass axe, keyboard, 375 px.
- **Docs:** README (production profile), runbook, threat model.
- **Owner decisions:** OTIO dependency; cost prices and budgets; image distribution and FFmpeg build; publication and reporting; who may access a shared deployment.
