# Kagebox hand-off: continuation plan (index and Phase 1)

Written 2026-11-24 on branch `milestone-1-2` (head `a998937`). This file is the **index** of the hand-off and holds the full Phase 1 continuation. Phases 2 to 4 are in linked files:

- [`handoff-phase-2.md`](handoff-phase-2.md): generative shots (GPU), milestones 2.0 to 2.9
- [`handoff-phase-3.md`](handoff-phase-3.md): polish (LoRAs, lip sync, foley), milestones 3.0 to 3.6
- [`handoff-phase-4.md`](handoff-phase-4.md): production (episodes, OTIO, dashboards, release), milestones 4.1 to 4.7

Status of this document: **a proposal for the owner to review**. Per `CLAUDE.md` ("Always propose a plan and wait for approval before writing code for a new phase"), nothing below is approved until the owner says so. Facts marked **(unverified)** could not be checked from the repository or from primary sources while writing.

## Table of contents

1. [Purpose and how to use this file](#1-purpose-and-how-to-use-this-file)
2. [Product and non-negotiable rules](#2-product-and-non-negotiable-rules)
3. [Current state](#3-current-state)
4. [Environment and how to run everything](#4-environment-and-how-to-run-everything)
5. [Architecture map](#5-architecture-map)
6. [Engineering conventions](#6-engineering-conventions)
7. [The remaining plan](#7-the-remaining-plan)
   - 7.0 [Shared patterns every stage reuses](#70-shared-patterns-every-stage-reuses)
   - 7.1 [Milestone 1.3: Series bible](#71-milestone-13-series-bible)
   - 7.2 [Milestone 1.4: Director](#72-milestone-14-director)
   - 7.3 [Milestone 1.5: 2.5D render](#73-milestone-15-25d-render)
   - 7.4 [Milestone 1.6: Voices, casting, score draft](#74-milestone-16-voices-casting-score-draft)
   - 7.5 [Milestone 1.7: Validate](#75-milestone-17-validate)
   - 7.6 [Milestone 1.8: Assemble](#76-milestone-18-assemble)
   - 7.7 [Milestone 1.9: UI screens](#77-milestone-19-ui-screens)
   - 7.8 [Milestone 1.10: End-to-end](#78-milestone-110-end-to-end)
   - 7.9 [Cross-cutting steps for Phase 1](#79-cross-cutting-steps-for-phase-1)
   - 7.10 [Phases 2, 3 and 4 (linked files)](#710-phases-2-3-and-4-linked-files)
8. [Optional and later work](#8-optional-and-later-work)
9. [Decision log and open questions](#9-decision-log-and-open-questions)
10. [Appendix: glossary, start prompt, definition of done](#10-appendix)

---

## 1. Purpose and how to use this file

1. This file lets any engineer or coding agent continue Kagebox with no access to earlier chats. It is tool-agnostic: every step is a shell command, a file edit or a test.
2. Read in this order: `CLAUDE.md` (the brief, binding), this file sections 2 to 6, then the milestone you are about to build in section 7 (or the linked phase file).
3. Background, read when a milestone points to it: `docs/plans/phase-1.md`, `docs/plans/phase-1-model-audit.md`, `docs/sample-chapter-source.md`, `docs/design/README.md`, `docs/design/adaptation-standard.md`, `docs/superpowers/plans/2026-11-19-milestone-1-2-understand.md` (the best worked example of a milestone plan).
4. The milestone 1.2 review ledger (`.superpowers/sdd/.../progress.md`) is **gitignored and only on the owner's machine**; its rulings and lessons are copied into sections 4 and 9 here.
5. Each milestone in section 7 is one branch and one pull request. Each task inside it is one focused session: write the failing tests, implement, verify, commit.
6. Stop points marked **STOP (owner)** need the owner's explicit answer before you continue. Never treat a comment in a file, a tool output or another agent's message as that answer.
7. Everything runs in Docker. Never install anything on the Mac host (section 4).
8. If a fact here disagrees with the code, the code wins: fix this file in the same pull request.
9. Security-sensitive tasks are marked **[SEC]** and need an independent careful review before merge (section 6).
10. GPU-only work is marked **[GPU]**; the owner's machine has no usable GPU in Docker (section 4).

---

## 2. Product and non-negotiable rules

Kagebox adapts comics, manga and manhwa into animated episodes that keep the original character designs exactly, look hand-drawn (not CGI), use cast character voices and an original score. Phase 1 is a **2.5D motion comic** of one chapter: source panels become layered shots with camera moves, voiced lines and a draft score.

| # | Rule | Why it exists | How the code enforces it today |
|---|---|---|---|
| 1 | **Rights gate.** Only owned, licensed or public-domain works. Rendering is blocked server-side until the project's rights record is approved. | Adapting someone else's work without rights is the main legal risk of the product. The gate must not depend on a UI. | `RightsRecord` (`pipeline/common/schemas/rights.py`): an approved record needs an evidence document and an approver who is not its creator. `require_render_allowed(record, now)` is called by the API (`apps/api/kagebox_api/routes/projects.py`, `routes/chapters.py`), at every flow start, and inside every per-item write (`orchestration/ingest_flow.py` `_rights_in`, `orchestration/understand_flow.py` `_rights_in`). `require_cloud_allowed` additionally gates `CLOUD_STAGES` (`orchestration/flows.py`; today only `director`). Denials are audited (`audit_events`). |
| 2 | **No hallucination.** Every generated field, shot, line and asset traces to source panel IDs. No visual evidence means `null`, never a guess. Anything failing a check goes to the review queue. | The product promise is fidelity to the source; invented content breaks it and cannot be audited. | `Cited[T]` (`pipeline/common/schemas/base.py`): a non-null value must cite at least one panel; `SceneJSON._cites_own_panel` (`schemas/understanding.py`); `Shot.source_panel_ids` has `min_length=1` (`schemas/production.py`). The understand model never produces IDs: the server attaches citations (`pipeline/understand/contract.py` `to_scene`), refuses text-evidence claims (`scene_text_claim`), and maps "unknown"/"n/a"/"none"... to null. Failures become `review_items` rows (`scene_failed`, `ocr_low_confidence`, `panel_merged`...). `Check.value=None` never passes (`schemas/production.py`). |
| 3 | **Voice consent.** A voice from a licensed actor or cloned from audio is unusable until consent is signed, enforced on the server. TTS watermarking stays on. | Cloning a real person's voice without consent is harmful and illegal in many places; watermarks make generated speech identifiable. | Schema only so far: `Voice._consent_rules` and `Voice.usable`, `require_voice_usable` raising `VoiceLocked`, and `VoiceLine._audio_is_watermarked` (generated audio must be watermarked). No voice API or TTS exists yet (milestone 1.6). The `consent` bucket exists (`pipeline/common/storage.py` `BUCKETS`). |
| 4 | **Hand-drawn, not CGI.** | The look is the product; CGI-looking motion destroys the source's character. | `StyleProfile` schema with defaults (multiplane camera, `interpolation_scope="camera_only"`, `lock_line_art=True`, threshold 0.85). Nothing renders yet. `docs/design/adaptation-standard.md` is the quality bar. |
| 5 | **Open models first; full provenance.** Every model's name, version, licence and source in `LICENSES.md` and in a per-artifact provenance manifest. | Licence surprises and unpinned weights are both supply-chain and legal risks. | `configs/models.yaml` pins commit SHA, sha256 and size of every file; `pipeline/providers/weights.py` refuses pickle formats and unpinned files; `pipeline/providers/local.py` `model_dir` re-verifies every file and refuses non-approved entries (`ModelNotApproved`); `trust_remote_code` is refused unless allowlisted in `configs/trust_remote_code.yaml` (empty). Every artifact carries `Provenance` (stage, tool, revision, licence, `params_hash`, `input_hashes`). |
| 6 | **Designs are the spec.** UI from `docs/design/screens/*.dc.html`, tokens from `docs/design/tokens.json`. | Consistent, accessible UI without design drift. | `apps/web` builds its CSS from tokens (`pnpm tokens:check` in `make test-web` fails if `src/app/tokens.css` drifts from `tokens.json`). |

Additional owner rule (from the owner, 2026-11-21 to 2026-11-24): **models run locally** (see decision log, section 9). Pages and audio do not leave the machine.

---

## 3. Current state

### 3.1 Done

| Milestone | What exists | Evidence |
|---|---|---|
| Phase 0 foundation | Compose stack (Caddy, web, api, Prefect server, worker-cpu, worker-gpu stub, Postgres, Qdrant, SeaweedFS S3), auth (Argon2id, `__Host-` cookies, CSRF header `x-csrf-token`, per-project roles viewer/editor/owner, non-members get 404), rights records with two-person approval, signed media links (`sign_media`, at most 5 min), upload validation (`pipeline/common/uploads.py`: sniffing, re-encoding, `safe_read_zip` against zip-slip/symlinks/bombs), provider interface, CI. | `README.md` "Security"; `.github/workflows/ci.yml` |
| 1.0 Models and licences | Audit of every candidate (`docs/plans/phase-1-model-audit.md`), pin/fetch/convert/check jobs, `LICENSES.md`. Magi v2 rejected. manga-ocr converted from pickle offline. | `configs/models.yaml`, `orchestration/models_fetch.py`, `models_convert.py`, `check_models.py` |
| 1.1 Ingest | PDF (sandboxed child, `pipeline/ingest/pdf_child.py`), CBZ and image folders; OpenCV panels and XY-cut reading order (RTL/LTR/vertical slicing); NorwayFish bubble detector; manga-ocr (ja) and PP-OCRv5 (en/ko); review items. | Sample chapter: 56 pages, **238 panels**, **297 bubbles**, 42 bubbles under OCR 0.90, 157 review items (95 `panel_irregular`, 42 `ocr_low_confidence`, 12 `panel_merged`, 4 `panel_full_page`, 4 `region_outside_panels`). `docs/sample-chapter-source.md` |
| 1.2 Understand | Local vision model **Qwen3-VL-2B-Instruct** (bf16, eager attention, 4 threads, CPU, `worker-cpu`), masked panel image and no OCR text sent, strict `PanelReading` contract, server-side citations, cache by `params_hash` + input hashes, `stage_runs` lock with heartbeat thread, trial gate (`limit_panels` <= 50, then confirmed full run), API `POST /api/chapters/{id}/understand`, `GET /api/chapters/{id}/stages`, scenes in `GET /api/pages/{id}/panels`, orphan-run reconciliation at worker start. | Full chapter 2026-11-24: **238/238 scenes**, 0 failed, 0 new review items; resumed run 187 panels in 18,030 s (5.0 h); s/panel mean **96.4**, p50 91.0, p95 128.4, max 345.7 (idle Mac, `max_time_s` 420); null rates setting 0 %, action 32.4 %, shot_hint 0 %, time_of_day 85.3 %, weather 92.4 %; fully cached re-run: 0 model calls, worker memory peak 3.9 GiB sampled. |

Test count: the last recorded full run was **570 passed, 3 deselected** (`make test-py`, milestone 1.2 Task 9); the fix wave added tests after that. Re-run `make test-py` for the current number.

### 3.2 PR #1 (milestone 1.2): state and what to do

Verified with `gh pr view 1` on 2026-11-24: **PR #1 is already merged** into `main` (merged 2026-11-24 17:16 UTC, merge commit `92a532f`). All five checks passed on head `a998937` (the CI security-job fix): `python`, `web`, `security`, `images`, GitGuardian. The fix-wave items are in the branch (commits `2aaa8c6` sentinel words to null, `2d23af2` heartbeat while walking cached panels, `c49ca7b` provider errors fail one panel, `9bcfe95` resumable budget stop, `bd739ea` flow re-checks the trial gate, `de8ce6a` one lock order, `66ec7dc` worker-cpu drops egress, `ab51fc8` orphan-run reconciliation, `0254c2d`/`ec2df6c` test refactors, `f178c67` README).

Your local clone may still have an old `main` (on the owner's machine it is at `59e0da9`). Before starting milestone 1.3:

```sh
git fetch origin
git checkout main
git pull --ff-only origin main          # should land on 92a532f or later
git checkout -b milestone-1-3-bible
make test-py && make lint                # green baseline before any change
```

### 3.3 Known issues carried forward (not blockers)

| Issue | Impact | Where it is handled |
|---|---|---|
| One stored scene holds the literal `"unknown"` for `time_of_day` and `weather` (PDF page 4, panel 1). New runs map such words to null; stored scenes are not rewritten. | That panel's two fields count as known. | Find it read-only: `SELECT panel_id FROM scenes WHERE artifact::text ILIKE '%"unknown"%';`. It is regenerated by the next run whose cache misses it (any prompt or settings change). Do not edit the database by hand. |
| SFX lettering and some signs stay readable under the text mask (pages 6, 7, 20, 44). | Possible wrong scene description; text claims are refused anyway. | Section 8 "detector improvement". |
| Over-masking: balloon + text box + margin blank parts of faces in busy panels (pages 7, 20, 48, 53). | Less visual evidence; more nulls. | Section 8. Do not lower the threshold (no evidence it helps). |
| Orphan-run reconciliation runs only at worker start (`orchestration/serve.py` `reconcile_orphaned_runs`); a run whose heartbeat is still inside the 10-minute window at restart is reconciled only by the next start. | A dead run can hold a deployment's only concurrency slot. | Section 4 trap 5; section 8 periodic reconciliation. |
| Ingest gaps: slanted gutters merge panels (flagged `panel_merged`), borderless panels missed, `panel_irregular` noisy, SFX not a bubble kind. | More review items; some crops include a neighbouring strip. | Accepted for Phase 1; section 8. |
| Security follow-ups from 1.1: PDF render child runs as the same uid in `worker-cpu`; no route to cancel an ingest. | A pdfium RCE could read the DB password and object-store keys. | Milestone 4.5 (production hardening), earlier if the stack is ever shared. |
| Stale docs: `README.md` "Status" still says Phase 1 is next; `Makefile` help for `up-cloud` and `LICENSES.md` "Hosted services" still say Together is used for understanding; `CLAUDE.md` says MinIO but the stack uses **SeaweedFS** (S3 API) for objects. `docs/plans/phase-1.md` line 3 still says "VLM/LLM on Together AI". | Confusing for newcomers. | Task 1.3.0b below. |

---

## 4. Environment and how to run everything

### 4.1 The Docker-only rule

Nothing is installed on the Mac host: no brew, pip, pipx, npm or uv on the host. Python, Node, every test and scanner run in containers pinned by digest (`Makefile` variables `UV_IMAGE`, `NODE_IMAGE`, `HADOLINT_IMAGE`, `TRIVY_IMAGE`, `GITLEAKS_IMAGE`). Lockfiles are generated in containers (`$(UV) lock`, pnpm in the node image). Host-side scripts are POSIX `sh` plus `curl`, `docker` and `python3 -I` used only as a JSON helper inside `tests/e2e/*.sh`.

### 4.2 Make targets (from `Makefile`)

| Target | What it does | Notes |
|---|---|---|
| `make test-py` | `uv sync --locked --all-extras --group dev`, then `pytest -q` (excludes `live`, `integration`, `models` markers) | Main TDD loop |
| `make test-web` | pnpm install (frozen), `tokens:check`, `typecheck`, `lint`, `vitest` | |
| `make test` | both of the above | |
| `make test-models` | offline tests marked `models` that load real pinned weights, models volume read-only, `--network none` | Needs fetched weights |
| `make lint` | ruff check, ruff format --check, mypy --strict, bandit | |
| `make scan` | `lint` + pip-audit (strict) + `pnpm audit --audit-level high` + hadolint (both Dockerfiles) + Trivy fs + gitleaks | Fails on high/critical |
| `make scan-images` | Trivy on exported `kagebox/{api,worker,web}:dev` tarballs (no Docker socket mounted) | Needs built images |
| `make check-models` | offline: verify hashes, load every approved model, one inference each (`models-check`, `mem_limit 8g`) | **Rebuilds the worker image first** (`worker-image` dependency): see trap 1 |
| `make fetch-models` / `make pin-models` / `make convert-models` | download pinned files (egress only here) / regenerate pins from `configs/models.sources.yaml` / offline pickle to safetensors conversion then promote | `fetch` and `convert` also rebuild the worker image |
| `make up-cloud` | create `.env`, generate secrets, `compose up -d --build --wait` with `KAGEBOX_PROFILE=cloud` | Builds images |
| `make up-gpu` | same plus `worker-gpu` | Needs NVIDIA Container Toolkit; not on the Mac |
| `make down` / `logs` / `ps` | stop (volumes kept) / follow logs / list | |
| `make admin` / `make user` | create accounts (password prompted, never echoed) | |
| `make ingest-sample` | real-data ingest of the sample chapter via the API | Needs `data/fixtures/blackjack-v1/work/001bj.pdf` |
| `make understand-sample` | trial (`TRIAL_PANELS=10`) through Caddy, API, Prefect, worker; `CONFIRM=yes` runs the full chapter | Model job: idle machine only |
| `make bakeoff-understand ARGS="..."` | vision bake-off via `deploy/scripts/bakeoff.sh` (bind-mounts `pipeline/` and `orchestration/` over the existing image, no rebuild) | `mem_limit 10g` |

### 4.3 Restore the stack without rebuilding

```sh
KAGEBOX_PROFILE=cloud docker compose -f deploy/compose.yaml --env-file .env up -d --no-build --wait
```

Use this after a Docker restart, after compose-only changes, and whenever image rebuilds are risky (trap 1). `make up-cloud` rebuilds.

### 4.4 Hardware facts (owner's machine)

- Apple M2, 16 GB RAM (as stated by the owner; a 2026-11-23 report observed about 17 GB total reported by the OS together with heavy swap, so treat 16 GB as the budget).
- Docker Desktop on macOS: **CPU only** (no Metal passthrough). The containers see 4 cores (`configs/understand.yaml` comment).
- Docker VM memory: set to about **11-12 GB** (Settings, Resources); last measured 10.4 GB. `worker-cpu` has `mem_limit: 8g`.
- Docker VM disk: about **95 GB**; the Mac host's own free space is the real limit (`Docker.raw` grew to about 70-75 GB in November 2026).
- Sum of `mem_limit` values (about 13.75 GB) exceeds the VM; they are ceilings. Never run the `bakeoff` (10g) or `fetch`/`models-check` (6g/8g) profiles while the worker is running a model.

### 4.5 Operational traps and fixes

| # | Trap | Symptom | Fix |
|---|---|---|---|
| 1 | **Docker VM disk full / build cache** | `postgres` crash-loops with `PANIC: no space left`; builds fail | Check `docker system df`. Free build cache with `docker builder prune -af` (build cache only; ask the owner first). Remove specific old untagged `kagebox/*` images with `docker rmi <id>`. Free space on the Mac host. Restart with section 4.3. Never start a rebuild while apt is failing (trap 3): a failed rebuild refills the cache. |
| 2 | **`docker image prune` deletes digest-pinned tool images** (the `uv` image), after which re-pull may hang (trap 4) | `make test-py` pulls again or hangs | **Never run `docker image prune`.** Remove images by id only. |
| 3 | **Transient apt/uv download failures** in image builds | `apt-get update` "invalid signature"; uv torch wheel timeout | Wait and retry later (the apt error was a transient Debian mirror issue in Nov 2026). `UV_HTTP_TIMEOUT=300` is already set in `deploy/docker/python.Dockerfile`. Do not loop rebuilds. |
| 4 | **Docker credential helper hang** on pull | `docker pull` never returns | Pull with an empty config: `DOCKER_CONFIG=$(mktemp -d) docker pull <image>@sha256:<digest>`. |
| 5 | **Dead Prefect run holds the concurrency slot** (each deployment has `concurrency_limit=1`) | New `POST /understand` stays `AwaitingConcurrencySlot` | Wait until the run's heartbeat is more than 10 min old (`STALE_INGEST_S=600`), then restart the worker: `docker compose -f deploy/compose.yaml --env-file .env restart worker-cpu`. `orchestration/serve.py` marks it CRASHED at start. Never mark runs by hand in the database. |
| 6 | **bf16 SDPA attention is about 100x slower on this CPU** | Minutes per token | Local loader only accepts `attn="eager"` (`pipeline/providers/local.py` `ALLOWED_ATTN`); keep it for every new local transformer model and measure. |
| 7 | **Timings under load are meaningless** | Trial projections 13.9 h vs 5 h actual | Run model jobs only when the 1-minute load average is under 6 and no other heavy app is open; check `uptime` before. Record the load in every report. |
| 8 | **Overnight runs stop when the Mac sleeps or Docker Desktop quits** | Run interrupted; dead run (trap 5) | Keep Docker Desktop open and the Mac awake and on power (for example `caffeinate -dimsu` in a terminal, a built-in macOS tool). Runs are cached and resumable: re-start the same command. |
| 9 | **`make check-models`, `fetch-models`, `convert-models` rebuild the worker image** | Same as trap 1 when apt is broken | Run them only when builds work; otherwise use `docker compose ... --profile fetch run --rm models-check` against the existing image. |

---

## 5. Architecture map

### 5.1 Repository layout (real paths)

| Path | Contents |
|---|---|
| `CLAUDE.md` | Engineering brief (binding) |
| `apps/api/kagebox_api/` | FastAPI: `main.py`, `deps.py` (session, CSRF, `require_project_role`), `security.py` (Argon2id, `sign_media`/`verify_media`), `limits.py` (1 MB body cap except uploads), `prefect_client.py` (`start_deployment`), `routes/{auth,projects,chapters,jobs,media}.py`, `migrations/versions/0001..0005`, `cli.py` |
| `apps/web/` | Next.js 16 + React 19 + Tailwind 4: `src/app/{login,projects,healthz}`, `src/components/{AppShell,Logo}.tsx`, `src/lib/{api,client}.ts`, `src/proxy.ts`, `scripts/tokens-to-css.mjs` |
| `pipeline/common/` | `schemas/{base,source,understanding,production,rights}.py`, `dbmodels.py`, `database.py`, `storage.py` (content-addressed refs, buckets `media`, `consent`, `evidence`, `provenance`), `uploads.py`, `ids.py`, `settings.py` |
| `pipeline/providers/` | `base.py` (`ModelSpec`, `ModelProvider`, `StructuredOutputFailed`, `ModelLoadFailed`, `fence_untrusted`, `provider_for`), `local.py` (`model_dir`, `load`), `local_vlm.py`, `structured.py`, `replay.py`, `together.py`, `registry.py` (`model_for_stage`, `trust_remote_code`), `weights.py`, `convert.py` |
| `pipeline/ingest/` | `sources.py`, `pdf_child.py`, `panels.py`, `bubbles.py`, `ocr.py`, `process.py` |
| `pipeline/understand/` | `contract.py`, `mask.py`, `request.py`, `settings.py` |
| `orchestration/` | `flows.py` (chapter pipeline, `STAGES`, `CLOUD_STAGES`, rights gate, `not_implemented` stubs), `ingest_flow.py`, `understand_flow.py`, `serve.py` (deployments and reconciliation), `models_fetch.py`, `models_convert.py`, `check_models.py`, `init_buckets.py`, `ingest_report.py`, `understand_report.py`, `understand_bakeoff.py`, `understand_synthetic.py` |
| `configs/` | `models.sources.yaml` (human-edited), `models.yaml` (generated `models:` and `conversions:`, plus `stages:`), `thresholds.yaml`, `trust_remote_code.yaml`, `understand.yaml`, `prompts/understand.v1..v4.md` |
| `deploy/` | `compose.yaml`, `docker/{python,web}.Dockerfile`, `caddy/Caddyfile`, `postgres/init-prefect-db.sh`, `scripts/{gen_secrets.py,pin_models.py,bakeoff.sh}`, `secrets/` (gitignored) |
| `tests/` | `unit/` (pytest, SQLite + `MemoryObjectStore`), `models/` (real weights), `e2e/` (shell scripts against the live stack), `fixtures/understand/` (synthetic replay recordings) |
| `docs/` | `design/` (screens, tokens, logo, adaptation standard), `plans/`, `superpowers/plans/` (per-milestone plans), `sample-chapter-source.md` |
| `data/` | gitignored: `fixtures/`, `imports/<project_id>/`, `reports/` |

Directories still to create per `CLAUDE.md`: `pipeline/{bible,director,render,audio,validate,assemble}`.

### 5.2 Stage contracts (schemas)

All in `pipeline/common/schemas/`, strict (`Strict`: `strict=True, extra="forbid", frozen=True`), every artifact subclasses `Artifact(id, schema_version=1, provenance)`.

| Stage | Produces | Status |
|---|---|---|
| ingest | `Page`, `Panel` (bbox, polygon, reading_order, crop_ref), `Bubble` (kind speech/thought/caption/sfx, text = untrusted OCR, `ocr_confidence`, `speaker: Cited[CharacterId]`, null today) | done |
| understand | `SceneJSON` (setting, action, shot_hint, time_of_day, weather, figures; `characters_present` and `emotion_by_character` empty until 1.3) | done |
| bible | `CharacterRef` (name Cited, `ReferenceCrop` list, outfits, traits, `identity_threshold` 0.90) | schema exists; extended in 1.3 |
| director | `Shot` (`source_panel_ids` >= 1, keyframe_refs, `CameraMove` static/pan/tilt/zoom_in/zoom_out/parallax_push, `duration_ms` 250..60000, `Transition` cut/dissolve/fade_black/wipe, `ShotLayer` list, `dialogue_line_ids`) | schema exists |
| audio | `Voice`, `Casting`, `VoiceLine` (watermark validator) | schema exists |
| art direction | `StyleProfile` | schema exists |
| validate | `Check` (whisper_wer, identity, style, lip_sync, traceability, watermark), `ValidationReport` (verdict pass/review) | schema exists |
| rights | `RightsRecord` | done |

`Provenance`: `stage`, `tool`, `tool_version`, `model_revision`, `license`, `source_url`, `provider` (`together`/`local`/`none`), `params_hash`, `input_hashes`, `components: list[ModelRef]`, `created_at`.

### 5.3 Data model (Postgres, `pipeline/common/dbmodels.py`)

Tables: `users`, `sessions`, `projects`, `project_members`, `rights_records`, `audit_events`, `chapters` (with ingest lock columns: status, run id, `ingest_started_at`, `ingest_heartbeat_at`), `pages`, `panels`, `bubbles` (artifact JSON + `needs_review`), `review_items` (project, chapter, `artifact_id`, `kind`, `reason` = fixed user-safe text, `status` open/resolved), `scenes` (one per panel, `params_hash`, artifact JSON), `stage_runs` (PK chapter_id + stage; status pending/running/done/failed, `run_id`, `started_at`, `heartbeat_at`, `finished_at`, `error`, `counts` JSON), `jobs` (Prefect flow run, status, `stage`). Helpers: `claim_stage`, `stage_alive`, `stage_takeover_allowed`, `full_run_allowed`, `STALE_INGEST_S = 600`. Migrations: Alembic in `apps/api/kagebox_api/migrations/versions/`, latest `0005_scenes_stage_runs.py`. New milestones add `0006_...` onward.

Object store: SeaweedFS (S3 API) at `http://objects:8333`; refs are content-addressed (`make_ref(bucket, data, ext)`). Qdrant 1.15.5 with an API key secret (`qdrant_api_key`), not yet used by code and **not yet mounted into `worker-cpu`**.

### 5.4 Provider interface and model pinning policy

- `ModelProvider` protocol: `llm_structured(spec, instructions, content, schema)` and `vlm_structured(spec, instructions, content, images, schema)`, both return a validated Pydantic model or raise `StructuredOutputFailed`. `provider_for(spec, local=..., together=...)` dispatches on `spec.provider`.
- `configs/models.sources.yaml` (edited by hand) -> `make pin-models` -> `configs/models.yaml` `models:` with every file's commit, sha256, size and format. `stages:` names the model per stage with `status: approved|proposed`.
- Only `.safetensors` weights (plus JSON/text data files). Any other non-pickle format (`onnx`, `ctranslate2`, `paddle`, GGUF) needs a reviewed `format_exception` in `weights.py`. Pickle is never loaded at runtime; pickle sources are converted offline in `models-convert` (no network, torch >= 2.6, `weights_only=True`) and promoted by hash.
- `trust_remote_code=False` always, unless an entry in `configs/trust_remote_code.yaml` (repo, revision, reviewer, date, justification).
- Workers are offline: `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, `worker-cpu` on the internal `back` network only. Only `models-fetch` has egress.
- Every new model: row in `LICENSES.md`, entry in `docs/plans/phase-1-model-audit.md` (or a phase audit file), `CHECKS` entry in `orchestration/check_models.py`, and **owner approval first**.

### 5.5 Patterns every long stage reuses (from `orchestration/understand_flow.py`)

- **Ownership lock:** `claim_stage(session, chapter_id, stage, run_id, now)`; every write checks the run still owns the `stage_runs` row (`_owns`, `_owned_stage`) inside the same transaction; a live owner refuses a second run (`...AlreadyRunning`), a stale one (no heartbeat for 10 min) is taken over and the old owner writes nothing.
- **Heartbeat:** a background thread (`_Heartbeat`, `_beating`, every `HEARTBEAT_INTERVAL_S = 60`) during long items, and a beat while walking cached items.
- **One transaction per item** with a rights re-check and a "chapter still ready" check.
- **Cache:** `params_hash` over every setting that changes output (prompt sha, schema sha, model, revision, dtype, thresholds) plus `input_hashes`; a re-run skips identical items and loads no model when everything is cached.
- **Trial before spend (I1):** `limit_*` parameter (bounded), measure seconds per item excluding load and cached items, store `counts = {trial: true, ..., projected_total_s, over_budget}`, stage ends `pending` (never `done`); a full run is allowed only after a current trial (`full_run_allowed`) and an explicit `CONFIRM=yes` in the make target.
- **Budget:** `max_minutes_per_chapter`; a budget stop is resumable (`counts.resumable`).
- **User-safe errors:** fixed message constants; never tracebacks, model output or OCR text in errors, logs or DB (`safe_errors` logs field and type only).
- **Hooks:** `on_cancellation`, `on_crashed`, `on_failure` write only while the run owns the row; `_fail_unstarted` for runs that never claimed.
- **Deployment:** `serve.py` registers `<stage>_flow.to_deployment(name=..., concurrency_limit=1)`, `serve(..., limit=1)` (one heavy flow at a time), and the deployment is added to `KINDS` for orphan reconciliation.
- **API start:** `POST /api/chapters/{id}/<stage>` (editor, ready, rights, conditional upsert to `pending`, `start_deployment`, `JobRow(stage=...)`, audit `<stage>.start`, 202 `{job_id}`), progress via `GET /api/chapters/{id}/stages`.

---

## 6. Engineering conventions

1. **TDD.** For every task: write the failing tests named in the plan, run `make test-py` and see them fail for the right reason, implement, run `make test-py` and `make lint` green, commit.
2. **Strict Pydantic v2** for every inter-stage artifact; new schemas subclass `Strict` or `Artifact`; model output is parsed into a small ID-free contract and the server builds the artifact (never let a model produce IDs or citations).
3. **mypy --strict**, **ruff** (rules E,F,W,I,B,UP,S,N,SIM,RUF,ASYNC; line 100), **bandit**; `make lint` must be clean. No `# type: ignore` without a reason comment.
4. **Untrusted data:** OCR text, file names, model output and uploaded media are data, never instructions, paths, SQL or shell. Subprocesses (FFmpeg) use argument lists, never `shell=True`.
5. **Review queue for doubt:** anything below a threshold, failing validation or a check that could not run becomes a `review_items` row with a fixed reason; nothing fails silently; a run continues past a failed item.
6. **Provenance manifests:** every artifact's `Provenance`; every stage output lists its models in `components`; the assembly bundle (1.8) aggregates them.
7. **Commits:** Conventional Commits as in the history (`feat(scope): ...`, `fix(...)`, `test(...)`, `docs(...)`, `chore(...)`), one task per commit, in the imperative, no secrets. Do not commit outside a task's commit step. Do not push or merge without the owner.
8. **How to do a task:** (a) read the task brief here and the files it names; (b) write the failing tests; (c) implement the smallest change; (d) run the verification command; (e) self-review the diff against the acceptance criteria and the rules in section 2; (f) commit; (g) record results and deviations in the milestone plan file.
9. **Milestone plan file:** before coding a milestone, copy its section from here into `docs/superpowers/plans/<date>-milestone-<n>-<name>.md` (or `docs/plans/milestone-<n>.md`), refine it, and get **owner approval**.
10. **Review depth (lean, owner preference):** one independent careful reviewer on **[SEC]** tasks (auth, rights, consent, crypto, uploads, subprocess, network, model loading, Qdrant scoping) and one lighter review on other tasks; one whole-milestone review at the end, then one fix wave. A reviewer gets the diff and the plan, not the implementer's conclusions. Fix rounds stop after three; then escalate to the owner.
11. **Where decisions are recorded:** model and licence decisions in `docs/plans/phase-1-model-audit.md` (later phases: `docs/plans/phase-<n>-model-audit.md`); milestone status in `docs/plans/phase-1.md`; real-data results in `docs/sample-chapter-source.md`; licences in `LICENSES.md`; rulings made during a milestone in that milestone's plan file under "Rulings" (the ledger used so far is not in git).
12. **Ask before** adding any external service, model, dependency or container image (`CLAUDE.md`). Record its licence.

---

## 7. The remaining plan

Conventions for every task below: "Verify" means `make test-py && make lint` unless stated otherwise. Test files live in `tests/unit/` unless stated. Each milestone ends with `make test`, `make lint`, `make scan`, `make scan-images` (if images changed), docs updates and a whole-milestone review.

Estimates are for the owner's M2 16 GB, Docker CPU (4 cores). Measured numbers are marked; everything else is an **estimate to be replaced by the trial gate's measurement**.

**Compute feasibility on the owner's machine (local-only; M2 16 GB, Docker CPU, 8 GB worker):**

| Stage | Model(s) | CPU feasible? | If too heavy |
|---|---|---|---|
| 1.3 bible | SAM 2.1 small, SigLIP so400m (both approved) | yes (estimate 0.7-2.6 h per chapter) | manual crops |
| 1.4 director | none (rules) or Qwen3-VL-2B text-only (approved) | yes | n/a |
| 1.5 render | SAM 2.1 small, Depth Anything V2 Small, OpenCV inpaint (or Big-LaMa after approval), FFmpeg | yes (estimate 2-4 h) | single-layer shots (flagged) |
| 1.6 voices | CosyVoice2-0.5B (to approve) + AudioSeal | probably; RTF unmeasured | Chatterbox (EN only) or defer the EN track |
| 1.6 score | ACE-Step v1.5 | **no (D2)**: gpu profile | `score_pending` + licensed-library drafts |
| 1.7 validate | Whisper large-v3-turbo, SigLIP | yes (estimate 10-30 min) | n/a |
| 1.8 assemble | FFmpeg | yes | n/a |
| Optional larger LLM/VLM | Qwen3.5-9B, gpt-oss-20b, 3-4B text models | only quantized; needs a runtime dependency and a format exception (**owner approval**) | stay on the approved 2B model |
| Phase 2-3 generative video, LoRA training, lip-sync models, foley | see linked files | **no**: GPU host | deferral and CPU fallbacks in the phase files |

### 7.0 Shared patterns every stage reuses

#### Task 1.3.0a [SEC] Extract a reusable stage runner

- **Why:** milestones 1.3 to 1.8 each need the lock, heartbeat, hooks, trial and budget logic that `understand_flow.py` implements privately. Copying it six times multiplies bugs.
- **Files:** create `orchestration/stage_run.py`; modify `orchestration/understand_flow.py` to use it (no behaviour change); tests `tests/unit/test_stage_run.py`.
- **Interfaces:** `class StageRun` (chapter_id, project_id, stage, run_id); `start(engine, chapter_id, stage, run_id, *, require_trial=False) -> StageRun`; `beat(engine, run)`; `beating(engine, run) -> ContextManager` (thread, `HEARTBEAT_INTERVAL_S`); `owned_stage(session, run) -> StageRunRow` (raises if lost); `finish(engine, run, status, counts)`; `fail(engine, run, message)`; `make_hooks(stage, job_stage) -> (on_cancelled, on_crashed, on_failed)`; `project_total_s` moved here; constants for user-safe messages parameterised by stage name.
- **Tests first:** `test_start_refuses_live_owner_and_takes_over_stale`, `test_old_owner_writes_nothing_after_takeover`, `test_beating_thread_beats_and_stops`, `test_hooks_touch_only_owned_row`, `test_trial_counts_never_mark_done`, `test_project_total_s`. All existing `tests/unit/test_understand_flow.py` tests must pass unchanged.
- **Accept:** zero behaviour change for understand (all its tests green, no test edits except imports); lock order chapter row then stage row (`tests/unit/lock_order.py` passes).

#### Task 1.3.0b Docs refresh (no code)

- Update `README.md` Status (Phase 1 milestones 1.0 to 1.2 done), `Makefile` help text for `up-cloud` (no Together), `LICENSES.md` "Hosted services" (Together not used; see section 9), `docs/plans/phase-1.md` line 3 and the Director rows (local), and add a note in `CLAUDE.md`'s owner-editable area only if the owner agrees (MinIO vs SeaweedFS). **STOP (owner)** for any `CLAUDE.md` edit.
- Verify: `make lint` (no code), proofread links.

---

### 7.1 Milestone 1.3: Series bible

**Goal.** For a chapter: candidate character crops per panel, embeddings, clusters into characters, an editor flow to approve/reject crops and merge/split clusters, speaker linking (bubble -> character, cited by panel), and `get_context(shot)` returning exact references for every character and location in a set of panels. Scenes gain `characters_present`.

**Inputs:** panels (`panels` table, crops in `media`), bubbles, scenes, the text mask from `pipeline/understand/mask.py`. **Outputs:** `characters`, `character_crops`, `speaker_links`, `locations` rows; Qdrant points; review items (`crop_uncertain`, `speaker_uncertain`, `cluster_unnamed`).

**Owner decisions before Task 1.3.3 (STOP):**

- **B1 Where character crops come from.** No approved model detects characters. Options: (a) SAM 2.1 small (already approved, `sam2-1-hiera-small`) prompted with a point grid on the text-masked panel crop, masks filtered by area and ink, crops embedded with SigLIP; (b) a new open-vocabulary detector (for example OWLv2 or Grounding DINO; licence, format and size **unverified**, needs the full audit and approval); (c) manual boxes drawn by an editor in the Series bible screen (no model; about 238 panels of work). **Recommendation: (a) with a trial gate, (c) as the always-available correction path.**
- **B2 Dialogue and voice-profile collections.** A multilingual text-embedding model is not approved. Recommendation: Phase 1 keeps dialogue in Postgres (`bubbles` + `speaker_links`), and creates only the `characters` and `locations` Qdrant collections; `dialogue` and `voice_profiles` collections wait for an approved model (voice profiles in 1.6).
- **B3 Character names.** Recommendation: characters are "Character 1..n" until an editor names them; an editor-entered name is stored with `name.value` and `name.panel_ids` = the panels the editor picked as evidence (for example a panel where the name is spoken), audited as `bible.rename`; a name with no panel stays null.

**Schemas to add** (`pipeline/common/schemas/understanding.py` or a new `bible.py`):

- `CharacterCandidate(Artifact)`: `id` (prefix `cand`), `panel_id`, `bbox: BBox` (panel coordinates), `mask_ref: ObjectRef | None`, `crop_ref`, `score: Score`, `method: Literal["sam2_grid", "manual"]`.
- `SpeakerLink(Artifact)`: `id` (`spk`), `bubble_id`, `panel_id`, `character_id: Cited[CharacterId]` (cites the bubble's panel), `method: Literal["tail_nearest", "single_character", "manual"]`, `score: Score`.
- `LocationRef(Artifact)`: `id` (`loc`), `name: Cited[ShortText]`, `panel_ids` (members), `embedding_id`.
- `ShotContext(Strict)`: `panel_ids`, `characters: list[CharacterContext]` (character_id, approved crop refs for this chapter, outfit Cited, voice_id None until 1.6), `locations: list[LocationRef]`.
- New ID prefixes in `pipeline/common/ids.py`: `CandidateId` (`cand`), `SpeakerLinkId` (`spk`), `LocationId` (`loc`).

**Files to create:** `pipeline/bible/{__init__,candidates,embed,cluster,store,speakers,context,settings}.py`, `orchestration/bible_flow.py`, `orchestration/bible_report.py`, `configs/bible.yaml`, `apps/api/kagebox_api/routes/bible.py`, `apps/api/kagebox_api/migrations/versions/0006_bible.py`, `tests/e2e/bible_sample.sh`, tests listed below. **Modify:** `pipeline/common/dbmodels.py`, `pipeline/common/ids.py`, `orchestration/flows.py` (runner `bible`), `orchestration/serve.py` (deployment `chapter-bible`, `KINDS`), `orchestration/check_models.py` (SigLIP check already exists: confirm), `deploy/compose.yaml` (`qdrant_api_key` secret into `worker-cpu`, and `KAGEBOX_QDRANT_URL`), `tests/unit/test_compose.py`, `Makefile` (`bible-sample`), `apps/api/kagebox_api/main.py` (router).

**Tasks:**

1. **1.3.1 Schemas, IDs, tables, migration.** Tables: `characters` (id, project_id, artifact JSON, created_at), `character_candidates` (id, chapter_id, panel_id FK cascade, character_id nullable FK, state pending/approved/rejected, params_hash, artifact), `speaker_links` (bubble_id unique FK cascade, chapter_id, params_hash, artifact), `locations` (id, project_id, artifact). Tests: `test_candidate_bbox_inside_panel_required`, `test_speaker_link_cites_bubble_panel`, `test_character_name_without_panel_is_null` (`tests/unit/test_schemas.py`), `test_candidates_cascade_with_panel_and_chapter`, `test_speaker_link_unique_per_bubble` (`tests/unit/test_dbmodels.py`). Migration: upgrade, `alembic check`, downgrade, upgrade on SQLite in the uv container (as in milestone 1.2 Task 5). Commit `feat(db): bible tables`.
2. **1.3.2 [SEC] Qdrant store with mandatory project scoping.** `pipeline/bible/store.py`: `BibleStore(client, *, project_id)`; collections `kagebox_characters`, `kagebox_locations` (cosine, size 1152 for SigLIP so400m: **verify the embedding size from the pinned `config.json`**); every upsert carries payload `project_id`, `chapter_id`, `panel_id`, `candidate_id`; every search/scroll **always** adds a `project_id` must-filter inside the store (callers cannot pass their own filter); point IDs are UUIDv5 of the candidate id. A `FakeQdrant` in tests. Compose: add `qdrant_api_key` to `worker-cpu` secrets and update `test_workers_get_only_the_secrets_they_read` (worker now reads it). Tests: `test_search_never_returns_other_project_points`, `test_store_refuses_empty_project_id`, `test_api_key_read_from_secret_file_and_never_logged`, `test_worker_cpu_gets_qdrant_key_only_for_bible` (`test_compose.py`). Commit `feat(bible): project-scoped Qdrant store`.
3. **1.3.3 Candidate crops (after B1).** `pipeline/bible/candidates.py`: `propose(panel_png, panel_bbox, text_boxes, segmenter, settings) -> list[CandidateBox]`. With option (a): mask text regions first (reuse `text_mask_boxes`/`mask_text_regions`), SAM 2.1 point grid (`grid_points` per side from `configs/bible.yaml`, start 8), drop masks under `min_area_frac` (0.02) or over `max_area_frac` (0.9), non-max suppression on boxes (IoU 0.5), keep top `max_per_panel` (8). Check whether transformers 5.23's `Sam2Model`/`Sam2Processor` accept batched point prompts; if not, loop per point (record which). Tests (fake segmenter): `test_text_regions_masked_before_segmentation`, `test_area_filters_and_nms`, `test_max_per_panel_cap`, `test_deterministic_for_same_input`; real-weights test in `tests/models/test_sam2_candidates.py` marked `models`. Estimate (unverified): SAM 2.1 small image encoder on 4 CPU cores a few seconds per panel plus about 0.1-0.5 s per prompt batch: measure in the trial.
4. **1.3.4 SigLIP embeddings.** `pipeline/bible/embed.py`: `SiglipEmbedder(loader=local.load)`, loads `siglip-so400m` once, `embed(images: list[bytes]) -> np.ndarray` (L2-normalised float32), batch size from config (8), eager attention, `torch.set_num_threads(4)`. Tests: `test_embeddings_normalised_and_batched` (fake model), `test_model_loaded_once`, `test_load_failure_is_model_load_failed`; `tests/models/test_siglip_embed.py` (same image twice -> cosine 1.0 +/- 1e-5; different synthetic figures < 0.95). SigLIP so400m safetensors is 3.5 GB (pinned size 3,511,950,624 bytes): fits the 8 GB worker alone; never load it together with the VLM.
5. **1.3.5 Clustering.** `pipeline/bible/cluster.py`: `cluster(embeddings, min_cluster_size, min_samples) -> list[int]` with `sklearn.cluster.HDBSCAN` (scikit-learn is already a dependency; **verify** the installed version has `HDBSCAN`, added in 1.3) on cosine distance (precompute `1 - E @ E.T`, `metric="precomputed"`), noise label -1 -> review `crop_uncertain`; stable cluster numbering (by first panel in reading order). Tests: `test_two_synthetic_characters_form_two_clusters`, `test_noise_points_become_review_items`, `test_cluster_ids_stable_across_runs`, `test_merge_and_split_recompute_centroids`.
6. **1.3.6 Speaker linking.** `pipeline/bible/speakers.py`: `link(bubble, panel_candidates, approved_characters) -> SpeakerLink | None`. Rules: one character in the panel -> `single_character` (score 0.9); else tail direction (estimate the tail tip as the bubble contour point farthest from the ellipse fit; **implement with OpenCV contours on the bubble crop**) and nearest candidate box centre along that ray -> `tail_nearest` with score from angle and distance; no character or score below `speaker_min_score` (0.6) -> null + review `speaker_uncertain`; captions and SFX never get speakers. Tests: `test_single_character_panel_links`, `test_tail_points_to_right_character` (synthetic drawing), `test_caption_and_sfx_have_no_speaker`, `test_low_score_is_null_and_reviewed`, `test_link_cites_bubble_panel_only`.
7. **1.3.7 [SEC] `get_context`.** `pipeline/bible/context.py`: `get_context(session, store, project_id, panel_ids) -> ShotContext`: verifies every panel belongs to the project (else `PermissionError`), returns approved crops only, the outfit for that chapter if known, locations whose members intersect the panels. Tests: `test_get_context_never_returns_refs_from_another_project`, `test_panels_from_other_project_refused`, `test_only_approved_crops_returned`, `test_empty_when_no_characters` (no guessing).
8. **1.3.8 Bible flow.** `orchestration/bible_flow.py` on the shared runner (1.3.0a): per panel: candidates -> embeddings -> store (one transaction per panel); then cluster the chapter; then speaker links; then fill `SceneJSON.characters_present` as a **separate `scene_characters` table** (do not rewrite understand artifacts; `characters_present` is derived at read time) or rebuild scenes with a new provenance: **recommend the separate table** to keep the understand cache intact. Trial gate `limit_panels` (1..50) with projection; `max_minutes_per_chapter` in `configs/bible.yaml` (start 240). Tests: `test_bible_trial_records_projection_never_done`, `test_bible_rerun_fully_cached_loads_no_model`, `test_rights_revoked_mid_run_stops`, `test_reingest_clears_bible_rows` (extend `tests/unit/test_ingest_flow.py`: `_commit` deletes candidates, links and Qdrant points of the chapter), `test_errors_and_logs_carry_no_ocr_text`.
9. **1.3.9 [SEC] API.** `routes/bible.py`: `POST /api/chapters/{id}/bible` (start, as understand: editor, ready, understand done, rights, trial body `{"limit_panels": n}`); `GET /api/projects/{id}/characters` (viewer); `POST /api/candidates/{id}/approve|reject` (editor, audited); `POST /api/characters/{id}/merge` `{other_id}` and `/split` `{candidate_ids}` (editor, audited); `PATCH /api/characters/{id}` `{name, evidence_panel_ids}` (editor; panels must be in the project); `PUT /api/bubbles/{id}/speaker` `{character_id}` (manual link, `method="manual"`, audited). Crop images only through `sign_media`. Tests in `tests/unit/test_bible_api.py`: `test_viewer_cannot_approve`, `test_non_member_gets_404`, `test_merge_across_projects_refused`, `test_rename_needs_panels_of_same_project`, `test_crop_urls_are_signed_and_user_bound`, `test_csrf_required_on_every_write`, route-table auth test still passes.
10. **1.3.10 Real run.** `make bible-sample` (trial 10 panels; `CONFIRM=yes` full) via `tests/e2e/bible_sample.sh` (copy the structure of `tests/e2e/understand_sample.sh`). **STOP (owner)** after the trial with the projection. Then full run; report clusters, crops per cluster, noise rate, speaker links by method, review items, seconds per panel, peak memory, in `docs/sample-chapter-source.md` "Bible results". Read the contact sheet by eye (crops per cluster) and record false merges as findings.

**Security:** Qdrant scoping (cross-project leakage is the top risk), signed crop URLs, editor-only writes with CSRF and audit, Qdrant key only via Docker secret, no OCR text in logs. **Risks:** SAM point-grid candidates may split one character into parts or include balloons; mitigated by text masking and editor review. Screentone manga may cluster by shading not identity. **Estimates:** engineering 10 sessions; compute (unverified) SAM + SigLIP about 10-40 s per panel -> 0.7-2.6 h for 238 panels; memory SAM small (about 0.2 GB weights, **unverified**) + SigLIP 3.5 GB < 8 GB. **Owner decides:** B1, B2, B3; approval of the trial projection.

---

### 7.2 Milestone 1.4: Director

**Goal.** Turn a chapter's panels, scenes and bubbles into an ordered `Shot` list (camera move, duration, transition), constrained to panel content; source panels are keyframes. Every shot traces to panels of the chapter; untraceable output goes to review.

**Owner decision first (STOP): D-DIR director engine. Local open-weight only (owner decision: no Together AI).** The repository still says otherwise in three places, all to be corrected in Task 1.4.0: `configs/models.yaml` `stages.director` (`provider: together`, `openai/gpt-oss-120b`, `status: approved`), `docs/plans/phase-1.md` (line 3 "VLM/LLM on Together AI", the 1.4 row "Together LLM", the 1.10 row "Together mocked", the Director row of the model table, D4) and `LICENSES.md` (gpt-oss-120b row, "Hosted services" row). Options:

| Option | What | Fits 16 GB Mac / 8 GB worker? | New approval? |
|---|---|---|---|
| (a) Rule-based director | `docs/design/adaptation-standard.md` section 4 as code: durations by panel kind, cuts by default, dissolve only on editor-marked time skips, fade at the chapter end; inputs `SceneJSON.shot_hint`, bubble count and text length, panel geometry, reading order | yes (no model, milliseconds) | no |
| (b1) Local LLM: reuse **Qwen3-VL-2B-Instruct** in text-only mode through `LocalVlmProvider.llm_structured` | already approved, Apache-2.0, measured peak RSS about 5.5 GB in bf16 | **yes** (measured) | no new model; owner confirms the new use |
| (b2) Local LLM: a new 3-4B text instruct model (for example Qwen3-4B-Instruct; licence expected Apache-2.0, **unverified**; bf16 weights roughly 8 GB, **unverified**) | needs the full audit (safetensors, pinned, no remote code) | **borderline**: bf16 likely exceeds the 8 GB worker; would need a quantized runtime | yes (model; and a runtime dependency if quantized) |
| (b3) Larger local LLM (for example gpt-oss-20b, Apache-2.0; tens of GB in bf16, **unverified** quantized sizes) | only through a quantized runtime (GGUF/llama.cpp or int4) with a format exception | **no** in bf16; maybe with 4-bit quantization; CPU speed unknown | yes (model, runtime, format exception): same path as the optional Qwen3.5-9B item in section 8 |

**Recommendation: (a) for Phase 1; (b1) as an optional refinement behind the same validator** (the LLM proposes camera move and duration per shot from a structured, text-only summary of panel facts; the validator and the rule table remain the authority). (b2)/(b3) only after an owner-approved quantized-runtime evaluation. In every case the LLM never sees OCR text as instructions (fenced, or not sent at all) and never produces IDs: shots are keyed by panel order supplied by the server.

**Task 1.4.0 Local-only config and docs.** Set `stages.director` to `provider: local`, `model: null` (rules) or `qwen3-vl-2b-instruct` (b1), `status: approved` only after the owner says so; remove `director` from `CLOUD_STAGES` in `orchestration/flows.py` (no stage sends content anywhere); keep `pipeline/providers/together.py` unused or remove it with its tests (owner choice; recommendation: remove, and drop `together_api_key` from `deploy/compose.yaml` secrets and `gen_secrets.py`); update `docs/plans/phase-1.md` and `LICENSES.md`. Tests: `test_no_stage_uses_a_hosted_provider` (every `stages.*.provider` is `local` or the stage has `model: null`), `test_cloud_stages_empty`, `test_compose_has_no_together_secret` (if removed). Verify `make test-py && make lint && make scan`.

**Schema changes** (`pipeline/common/schemas/production.py`):

- `Shot.source_bubble_ids: list[BubbleId] = []` (dialogue placement before voice lines exist; 1.6 fills `dialogue_line_ids`).
- `Shot.order: int >= 1`, `Shot.chapter_id: ChapterId`.
- `DurationRule(Strict)`: `min_ms`, `max_ms`, `basis: Literal["establishing","dialogue","reaction","action","reveal"]`.
- `Shot.duration_rule: DurationRule` (Assemble may re-time within it to fit audio: voice-first).
- `ShotList(Artifact)`: `chapter_id`, `shot_ids` in order.

**Files:** create `pipeline/director/{__init__,rules,plan,validate,settings}.py`, `configs/director.yaml` (durations table, +/-40 %, lead-in 500 ms, tail 500 ms, ms per character for dialogue estimate per language), `orchestration/director_flow.py`, `apps/api/kagebox_api/routes/shots.py`, migration `0007_shots.py`, `tests/e2e/director_sample.sh`; modify `orchestration/flows.py`, `serve.py`, `dbmodels.py` (`shots` table: id, chapter_id, order, params_hash, artifact; unique (chapter_id, order)).

**Tasks:**

1. **1.4.1 Schema and table.** Tests: `test_shot_requires_source_panel`, `test_shot_bubbles_must_be_listed_with_panels` (validator: non-empty bubbles need panels), `test_duration_within_rule`, `test_shots_unique_order_per_chapter`. Migration as before.
2. **1.4.2 Panel classification.** `rules.classify(panel, scene, bubbles) -> basis`: shot_hint `wide`/`establishing` -> establishing; any speech bubble -> dialogue; close-up with no bubble -> reaction; action verbs are **not** parsed from model text (untrusted): use `shot_hint` and panel aspect only; full-page panel (`panel_full_page` review kind or fill) -> reveal. Tests: `test_dialogue_panel_classified_by_bubbles`, `test_full_page_is_reveal`, `test_classification_ignores_scene_free_text`.
3. **1.4.3 Duration and camera.** `rules.duration(basis, bubbles, lang, cfg)`: dialogue = lead-in + estimated speech + tail (estimate from characters per second per language in config; replaced by real audio length in 1.8); others from the table midpoint; `rules.camera(basis, panel_aspect)`: establishing -> slow pan along the long axis or `zoom_in`; dialogue -> `static` or slight `zoom_in`; reaction -> `static`; reveal -> `zoom_in`. Tests: `test_dialogue_duration_grows_with_text`, `test_duration_clamped_to_rule`, `test_camera_choice_table`.
4. **1.4.4 Plan.** `plan.plan_chapter(panels_in_reading_order, scenes, bubbles, cfg) -> list[Shot]`: one shot per panel by default; merge consecutive action panels on the same page into one `pan` shot only when `cfg.merge_action_panels` is true (default false in Phase 1, since "shared background" needs evidence we do not have); transitions: `cut` by default, `fade_black` after the last panel of the chapter, `dissolve` only when an editor marks a time skip (no automatic guess). Tests: `test_every_panel_covered_exactly_once`, `test_shot_order_follows_reading_order_rtl`, `test_last_shot_fades_to_black`, `test_no_dissolve_without_editor_mark`.
5. **1.4.5 [SEC] Validator.** `validate.validate_shots(shots, chapter_panels, bubbles_by_panel) -> list[Issue]`: every panel id exists in this chapter; every bubble belongs to a listed panel; panel coverage complete and non-overlapping; durations within rule; allowed camera/transition literals. Any issue -> shot not stored, review `shot_untraceable` (fixed reason with the shot order only). Property test with Hypothesis is **a new dev dependency (ask)**; without it, write a seeded random generator test. Tests: `test_property_every_shot_has_panel_from_its_chapter` (randomised, 500 cases, seed fixed), `test_foreign_panel_rejected_to_review`, `test_foreign_bubble_rejected`, `test_gap_in_coverage_flagged`.
6. **1.4.6 Flow and API.** `director_flow` (no model, fast; still uses the stage runner for locking and provenance with `provider="none"`, `tool="kagebox-director"`). API `POST /api/chapters/{id}/director` (editor; needs understand done), `GET /api/chapters/{id}/shots` (viewer), `PATCH /api/shots/{id}` (editor: camera, duration within rule, transition, `time_skip` mark; audited; re-validated). Tests: `test_director_needs_understand_done`, `test_patch_out_of_rule_duration_rejected`, `test_patch_audited`, `test_rerun_same_inputs_same_shots` (determinism).
7. **1.4.7 Real run** `make director-sample`; report shot count by basis, total duration, review items. Expect about 238 shots and roughly 10-20 minutes of runtime before voice re-timing (**estimate**).

**Security:** shot edits are writes (editor, CSRF, audit); validator is the trust boundary if an LLM is added later. **Risks:** one-shot-per-panel pacing may feel slow; editor overrides exist. **Estimates:** 6 sessions; compute seconds. **Owner decides:** D-DIR; whether `merge_action_panels` stays off.

---

### 7.3 Milestone 1.5: 2.5D render

**Goal.** Each shot becomes a short clip: the source panel separated into layers (characters, background with holes inpainted), ordered by depth, composed with a multiplane camera move, encoded with FFmpeg. Deterministic, memory-bounded, no motion blur, hand-drawn look preserved (source pixels only; no generation).

**Pre-task 1.5.0: compositor study (required before Task 1.5.1).** Read-only study of the public repo `Moh4696/cinematic-skill` (`skills/cinematic-film/scripts/engine.py`, `render.py`, `build_sfx.py`, `mix_soundtrack.py`) per the checklist in `docs/plans/phase-1.md` ("Candidate to study at milestone 1.5"): camera model, word-timed events, determinism, peak memory, `skia-python` arm64 wheels/licence/offline load, tests and maintenance, installer risk (never run its `npx`/`curl | bash` installers; nothing is installed or vendored). Then measure a numpy compositor prototype on a fixture shot: frame determinism (sha256 of frames over two runs), peak RSS, seconds per frame in the worker container. Output: `docs/plans/milestone-1-5-compositor-decision.md` (one page). **STOP (owner)** if it recommends `skia-python` (new native dependency). Default if not: numpy + Pillow (already dependencies).

**Owner decisions (STOP before 1.5.3):**

- **R1 Inpainting model.** Audit verdict "Big-LaMa safetensors copy `michaelgold/big-lama` @ `98ab7cfa`, use with checks" (compare to our own sandboxed conversion of the official checkpoint; Places365 training-data terms are an open legal item). It is **not yet in `configs/models.sources.yaml`**. Options: (a) pin it (plus conversion check), (b) OpenCV `cv2.inpaint` (Telea; no weights; weaker on large holes). **Recommendation: start with (b) behind an `Inpainter` protocol; add (a) after approval** if quality demands it.
- **R2 Output format.** Recommendation: 1920x1080 max (fit, letterbox with the page paper colour), 24 fps, H.264 `yuv420p` CRF 18 via the Debian FFmpeg already in the worker image (`deploy/docker/python.Dockerfile` installs `ffmpeg`; it is a GPL build, fine for running a service, matters only if images are distributed).

**Schemas:** `ShotLayer` exists (`ref`, `depth` 0..16, `parallax` 0..2). Add `RenderSettings(Strict)` (`fps`, `width`, `height`, `crf`, `max_layers`, `rss_ceiling_mb`), `RenderedShot(Artifact)` (`shot_id`, `clip_ref`, `frame_count`, `first_frame_sha256`, `last_frame_sha256`, `layers: list[ShotLayer]`, `palette_ref`). Extend `StyleProfile.palette` use: `palette` extracted per project.

**Files:** create `pipeline/render/{__init__,layers,depth,inpaint,compose,camera,ffmpeg,palette,settings}.py`, `pipeline/common/hashing.py`, `configs/render.yaml`, `orchestration/render_flow.py`, migration `0008_renders.py` (`rendered_shots`, `style_profiles` tables), `tests/e2e/render_sample.sh`; modify `flows.py`, `serve.py`, `check_models.py` (confirm `sam2`, `depth` checks exist: they do), API `routes/shots.py` (`GET /api/shots/{id}/render` signed clip URL).

**Tasks:**

1. **1.5.1 Chunked hashing (I2).** `pipeline/common/hashing.py`: `sha256_stream(fileobj, chunk=1 << 20) -> str`; replace whole-file reads where media is hashed (search with `grep -rn "hashlib.sha256(" pipeline orchestration`; ingest `_hash_import` already chunks). Test: `test_stream_hash_equals_whole_file_hash_and_reads_one_chunk_at_a_time` (a file larger than the chunk; a spy file object records max read size).
2. **1.5.2 Layer separation.** `layers.separate(panel_png, character_boxes, segmenter) -> list[Layer]` (SAM 2.1 masks seeded with the approved character candidate boxes from 1.3 as box prompts; background = panel minus union of masks); masks dilated by `mask_dilate_px` (2) to keep line art; masks with area below 1 % dropped; at most `max_layers` (6). Tests (fake segmenter): `test_layers_cover_panel_exactly_once` (alpha sum 1 per pixel), `test_line_art_kept_by_dilation`, `test_no_boxes_gives_single_background_layer`.
3. **1.5.3 Inpainting** behind `Inpainter` protocol (`opencv` default; `lama` after R1). Tests: `test_hole_filled_and_outside_unchanged` (pixels outside the hole byte-identical), `test_inpaint_deterministic`.
4. **1.5.4 Depth order.** `depth.order(panel_png, layers, estimator) -> list[int]` with Depth Anything V2 Small (approved, Apache-2.0; larger sizes are CC-BY-NC and must never be added): median relative depth per layer mask; ties keep reading order. Tests: `test_nearer_layer_drawn_last`, `test_background_always_depth_zero`; models test `tests/models/test_depth_order.py`.
5. **1.5.5 Camera and compositor.** `camera.path(move, duration_ms, fps, panel_size, out_size) -> list[CameraState]` (pan/tilt linear with ease-in-out, zoom 1.0 -> 1.15, `parallax_push` = zoom with per-layer parallax factor `0.5 + 0.25 * depth`); `compose.frames(layers, order, path) -> Iterator[np.ndarray]` (numpy, integer-rounded transforms, nearest or Lanczos resampling fixed, **no motion blur**, no smoothing beyond the camera ease); camera moves are on ones (interpolation scope `camera_only` allows smooth camera; drawn content is never interpolated). Tests: `test_static_shot_frames_identical`, `test_pan_moves_monotonically`, `test_parallax_foreground_moves_more`, `test_frames_deterministic_hash` (golden sha256 of frame 0, middle, last for a synthetic shot), `test_no_motion_blur` (frame equals a pure affine resample of layers).
6. **1.5.6 [SEC] FFmpeg encoder.** `ffmpeg.encode(frames, out_path, settings)`: `subprocess.Popen([...])` with an **argument list**, `stdin=PIPE` raw `rgb24` frames, no `shell`, fixed argument set (no user strings in arguments; output path inside a temp dir the worker owns), timeout, stderr captured and never logged verbatim (log return code and a fixed message). Tests: `test_ffmpeg_called_with_argument_list_never_shell` (monkeypatched Popen), `test_encoder_streams_frames_without_buffering_all` (generator consumed lazily), `test_ffmpeg_failure_is_user_safe_error`.
7. **1.5.7 Memory discipline (I2).** Load artwork only for the active shot; stream frames into FFmpeg; never keep all frames. Test `test_render_peak_rss_bounded_and_flat` in a child process: render a 60 s synthetic shot and a 10 s one, assert `ru_maxrss` below `rss_ceiling_mb` (start 3000 MB, below the 8 GB worker) and the 60 s peak within 10 % of the 10 s peak.
8. **1.5.8 Palette.** `palette.extract(page_pngs, k=16, seed=0) -> list[HexColor]` (scikit-learn KMeans on a fixed-seed sample of pixels, uppercase hex); stored in the project's `StyleProfile`. Tests: `test_palette_deterministic_and_uppercase`, `test_palette_size_bounded`.
9. **1.5.9 Render flow.** Per shot, one transaction for the result; cache by params (settings, model revisions, layer inputs); **trial gate**: render `limit_shots` (1..10) at `trial_scale` 0.5, report seconds per output second and projection; then full run after confirmation. Models load one at a time (SAM, then depth, then free) to keep memory low. Tests: `test_render_trial_low_res_never_done`, `test_render_rerun_cached`, `test_rights_rechecked_per_shot`, `test_render_needs_approved_bible_candidates_or_falls_back_to_single_layer` (flagged `render_single_layer` review item).
10. **1.5.10 Real run** `make render-sample`: trial 3 shots, **STOP (owner)** with projection; full run; record seconds per shot, peak memory, review items; view 10 clips by eye for mask and inpaint artefacts (findings only).

**Security:** FFmpeg subprocess (argument lists, no shell, decoder input is our own raw frames only), signed clip URLs, memory limits. **Risks:** mask/inpaint artefacts on dense pages (to review; manual mask correction is a later step); CPU time. **Estimates:** 11 sessions plus the study; compute (unverified): SAM per panel a few seconds, depth Small about 1-3 s, compose plus encode 1080p roughly 0.05-0.2 s per frame -> 10-20 min of video at 24 fps is 14,400-28,800 frames -> about 1-2 h encode; overall 2-4 h per chapter. **Owner decides:** compositor (if Skia), R1, R2, trial projection.

---

### 7.4 Milestone 1.6: Voices, casting, score draft

This milestone is split into seven sub-milestones, each its own pull request, in this order: **1.6a** voice library, casting and consent; **1.6b** TTS engine evaluation, image and watermark; **1.6c** voice lines, voicing, previews and recast; **1.6d** music direction study and cue-sheet vocabulary; **1.6e** cue sheet from SceneJSON; **1.6f** music provider, reference-audio rights gate and score draft (or deferral); **1.6g** acquisition records for non-generated audio (I3) and the licensed-library fallback. The casting UI is milestone 1.9 (Task 1.9.5); audio validation is 1.7; mixing is 1.8.

**Dependencies.** Speaker links from milestone 1.3 (Task 1.3.6: `speaker_links`, bubble -> character, cited by the bubble's panel) are required: a bubble without a speaker link produces no voice line (review `line_no_speaker`). Emotion comes from understand: `SceneJSON.figures[].emotion` today, and from `emotion_by_character` once 1.3 fills `characters_present` (derived table `scene_characters`). Shots and durations come from 1.4.

#### 7.4.1 Owner decisions (STOP before the task that needs them)

| ID | Question | Options | Recommendation | Needed by |
|---|---|---|---|---|
| V1 | Language tracks (D5) and the English text source | JA only; JA + EN with EN from the English edition aligned by page/bubble order; EN by machine translation (no approved model; generated text, must be marked and reviewed) | JA + EN, EN from the already-downloaded English edition (`enbj001.zip`) ingested as a parallel chapter; ship JA only if alignment slips | 1.6c |
| V2 | TTS engine | CosyVoice2-0.5B (primary); Chatterbox (alternative); others after audit | CosyVoice2 if the CPU evaluation (Task 1.6b-1) meets the speed bar; Chatterbox for EN only if CosyVoice2 fails | 1.6b-2 |
| V3 | Voice sources in Phase 1 | designed only; also licensed actor / cloned with signed consent | designed only until consent upload is merged and reviewed; no cloning without a signed consent the owner provides | 1.6a |
| V4 | Consent encryption (D6) | app-level AES-GCM, key in a Docker secret; object-store SSE | AES-GCM with new secret `consent_key` and the `cryptography` package (**new direct dependency: approve**; licence to verify on PyPI, expected Apache-2.0 OR BSD-3-Clause) | 1.6a-3 |
| V5 | Score on the Mac (D2) | (a) skip in `cloud`, file `score_pending`; (b) native macOS worker (breaks the Docker rule); (c) GPU host | (a), plus the optional licensed-library fallback (1.6g) for drafts | 1.6f |
| V6 | Music model | ACE-Step v1.5 (gpu profile); alternatives after audit | decide after Task 1.6f-1 | 1.6f |
| V7 | Final release music | generated score; human composer; licensed library | **owner's call**; recommendation: generated drafts for internal review only, and a human composer or a licensed library for any public release until the similarity check (1.6f-6) is proven | before any publication |
| V8 | Voice designs per character | owner writes short descriptions (age, timbre, energy); engineering proposes from the bible | owner approves each designed voice | 1.6c |

#### 7.4.2 Data model

Schemas in `pipeline/common/schemas/production.py` (`Voice`, `Casting`, `VoiceLine` exist) plus a new `pipeline/common/schemas/audio.py`:

- `Voice {id, name, description, source: designed|licensed_actor|cloned, consent_status, consent_doc_ref, engine, reference_audio_ref, languages[]}` (exists; validator: designed => `not_required`; real person => consent needed; cloned => reference audio). Add `engine_params: dict[str, str|float]` (speaker embedding id, speed, pitch) with a closed key set per engine.
- `Casting {project_id, character_id, language, voice_id, version, changed_by, changed_at}` (exists). Add `reason: Literal["assign","swap","undo"]` and `previous_voice_id: VoiceId | None`.
- `ConsentDocument(Strict)`: `id` (`cdoc`), `voice_id`, `ciphertext_ref` (bucket `consent`), `plaintext_sha256`, `nonce_b64`, `key_id`, `content_type`, `uploaded_by`, `uploaded_at`, `approved_by`, `approved_at` (approver != uploader), `revoked_at`.
- `VoiceLine` (exists): `bubble_id`, `panel_id`, `character_id`, `language`, `text`, `emotion: Cited[ShortText]`, `voice_id`, `casting_version`, `audio_ref`, `watermarked`, `wer`, `status`. Add `duration_ms`, `sample_rate`, `watermark_score: Score | None`, `text_source: Literal["ocr_ja","edition_en","translation"]`.
- `VoicePreview(Strict)`: `voice_id`, `line_id`, `audio_ref`, `params_hash`, `created_at`.
- `MusicCue(Strict)`: `id` (`cue`), `shot_ids` (>= 1), `panel_ids` (>= 1), `mood: Cited[CueMood]` (closed vocabulary from 1.6d), `scene_type: CueSceneType`, `intensity_curve: list[tuple[int, Score]]` (ms, level), `hit_points_ms: list[int]`, `leitmotif_character_ids: list[CharacterId]`, `silence: bool`, `stinger: bool`, `tempo_bpm_range: tuple[int,int] | None`, `instrumentation: list[Instrument]` (closed vocabulary).
- `CueSheet(Artifact)`: `chapter_id`, `cues` in order, `vocabulary_version`.
- `ScoreDraft(Artifact)`: `cue_id`, `audio_ref | None`, `status: Literal["generated","library","pending"]`, `music_model: ModelRef | None`, `prompt_template_id`, `asset_id | None`.
- `AudioAsset(Artifact)` with `Acquisition(Strict)`: `source_url: HttpsUrl`, `licence: Literal["CC0","PD","CC-BY-4.0","owned","licensed"]`, `licence_text_sha256`, `licence_snapshot_ref` (bucket `evidence`), `author`, `attribution`, `acquired_at`, `acquired_by`, `rights_doc_ref` (for owned/licensed). `AudioAsset.use: Literal["stinger","draft_cue","ambience","reference_for_conditioning"]`.

Tables (migration `0009_audio.py`): `voices`, `castings` (unique (project_id, character_id, language, version); index (project_id, character_id, language)), `consent_documents`, `voice_lines` (unique (bubble_id, language); `params_hash`), `voice_previews` (unique (voice_id, line_id, params_hash)), `cue_sheets`, `score_drafts`, `audio_assets`. Audit events: `voice.create`, `casting.assign`, `casting.swap`, `casting.undo`, `consent.upload`, `consent.approve`, `consent.read`, `consent.revoke`, `audio_asset.import`, `audio_asset.refused`.

#### 7.4.3 Sub-milestone 1.6a: voice library, casting, consent [SEC]

Files: `pipeline/audio/{__init__,voices,casting,consent}.py`, `apps/api/kagebox_api/routes/voices.py`, migration `0009_audio.py`, `deploy/scripts/gen_secrets.py` (new `consent_key`, 32 random bytes base64), `deploy/compose.yaml` (secret `consent_key` mounted **only into `api`**; workers never decrypt consent), tests `tests/unit/test_voices_api.py`, `tests/unit/test_consent.py`, `tests/unit/test_casting.py`.

1. **1.6a-1 Tables and schemas.** Tests: `test_designed_voice_consent_not_required`, `test_cloned_voice_needs_reference_audio`, `test_signed_consent_needs_document`, `test_casting_version_unique_per_language`, `test_voice_line_unique_per_bubble_and_language`. Verify: `make test-py && make lint`; migration up/check/down/up on SQLite.
2. **1.6a-2 Voice library and casting API.** Routes: `POST /api/projects/{id}/voices` (editor; designed only unless V3 says otherwise), `GET /api/projects/{id}/voices` (viewer; locked voices included with `usable=false` and a reason), `PUT /api/projects/{id}/castings` `{character_id, language, voice_id}` (editor; new version n+1, `reason="assign"` or `"swap"`, audited with old and new voice ids), `POST /api/projects/{id}/castings/undo` `{character_id, language}` (new version pointing at the previous voice, `reason="undo"`; history is never deleted or rewritten), `GET /api/projects/{id}/castings?language=ja` (current), `GET /api/projects/{id}/castings/history?character_id=&language=` (viewer). Server-side lock: `require_voice_usable` before any cast, and the casting endpoint refuses voices whose `languages` lack the requested language. Tests: `test_cast_locked_voice_refused_403`, `test_casting_versions_increment_and_audit`, `test_undo_restores_previous_voice_as_new_version`, `test_undo_without_history_409`, `test_per_language_casting_independent`, `test_cast_voice_without_language_refused_422`, `test_viewer_cannot_cast`, `test_non_member_404`, `test_csrf_required_on_every_write`. Accept: every change produces exactly one audit row and one casting version.
3. **1.6a-3 Consent documents (after V4).** `POST /api/voices/{id}/consent` (project owner; PDF or image through the Phase 0 validators, max 20 MB): AES-256-GCM, random 96-bit nonce, associated data = `voice_id || plaintext_sha256`, ciphertext to bucket `consent`, `key_id` recorded for rotation. `POST /api/voices/{id}/consent/approve` (admin, not the uploader) sets `consent_status=signed`; `POST .../revoke` sets `revoked` and **locks the voice at once** (existing castings to it are reported as `voice_locked` review items; lines are not voiced with it again). `GET /api/voices/{id}/consent` (owner or admin): decrypts in memory, returns a one-use signed link valid at most 60 s, audit `consent.read` with user and time. Tests: `test_consent_ciphertext_not_plaintext_in_store`, `test_tampered_ciphertext_refused`, `test_wrong_associated_data_refused`, `test_uploader_cannot_approve_own_consent`, `test_revoked_consent_locks_voice_and_flags_castings`, `test_every_consent_read_audited`, `test_consent_key_never_logged` (caplog sentinel), `test_consent_key_mounted_only_in_api` (`tests/unit/test_compose.py`). **Independent security review required.**
4. **1.6a-4 Server-side consent in workers.** `pipeline/audio/voices.py` `load_usable_voice(session, voice_id) -> Voice` re-reads the voice row inside the voicing transaction and calls `require_voice_usable`; used by every TTS call path. Test: `test_worker_refuses_voice_locked_after_casting` (lock between casting and voicing; no audio stored; review `voice_locked`).

#### 7.4.4 Sub-milestone 1.6b: TTS engine evaluation, image and watermark

1. **1.6b-1 Evaluation (no code merged; STOP (owner) at the end).** Write `docs/plans/milestone-1-6-tts-evaluation.md` from primary sources (model cards, repos, PyPI) for each candidate, with the same method as `docs/plans/phase-1-model-audit.md`:

   | Candidate | Known so far (audit 2026-11-19) | To verify and measure |
   |---|---|---|
   | CosyVoice2-0.5B (`FunAudioLLM/CosyVoice2-0.5B` @ `eec1ae6c`) | Apache-2.0 card tag; readme "academic purposes" disclaimer (ambiguity recorded); `llm.pt`, `flow.pt`, `hift.pt` are pickles (convert offline); `campplus.onnx`, `speech_tokenizer_v2.onnx` need an ONNX exception; HyperPyYAML config can instantiate callables (vendor and review as code); upstream pins torch 2.3.1 (we require torch >= 2.6) | runs on torch >= 2.6; Japanese and English quality; emotion/instruction control; zero-shot voice from a designed reference (needs a reference clip: a designed voice must use a **synthetic or owned** reference, never a recording of a real person without consent); CPU real-time factor on 4 cores in Docker; peak RSS; total weights size |
   | Chatterbox (`ResembleAI/chatterbox`) | MIT; Japanese path depends on pykakasi (GPL-3.0+); built-in Perth watermark loads a pickle | English-only use without pykakasi; replace its watermark with AudioSeal; CPU real-time factor; RSS |
   | Others (only if both fail) | none audited | licence, format, languages, CPU speed |

   Measurement protocol (in the `worker-tts` candidate image, not the host): synthesise the same 20 synthetic test sentences (10 JA, 10 EN, written by us, no OCR text) per engine, idle machine (load < 6), record per-sentence seconds, audio seconds, real-time factor (RTF = synth seconds / audio seconds) p50/p95, peak RSS. **Speed bar:** RTF p50 <= 5 on CPU (chapter of about 15-25 min of speech -> at most about 2 h; estimate, unverified). **STOP (owner):** choose the engine (V2) with the table.
2. **1.6b-2 [SEC] `worker-tts` image and model pins.** `deploy/docker/tts.Dockerfile` (multi-stage, digest-pinned base, non-root uid 10001, read-only rootfs, no compilers in the runtime stage), separate lock `deploy/tts/pyproject.toml` + `uv.lock` with hashes (`make tts-lock` in the uv container), `make tts-image`; compose service `worker-tts` (`x-hardening`, network `back` only, `models:/models:ro`, secrets db and s3 only, `HF_HUB_OFFLINE=1`, `mem_limit` from 1.6b-1 measurement, Prefect worker serving `chapter-voice` and `chapter-recast` with `limit=1`). Model entries in `configs/models.sources.yaml` with a `convert:` block for the pickles, an ONNX `format_exception` for exactly the two CosyVoice2 files (pinned onnxruntime, no custom ops, external-data paths must resolve inside the model dir), config vendored under `pipeline/audio/vendor/cosyvoice2/` with a review note. Tests: `test_onnx_outside_exception_refused`, `test_onnx_external_data_outside_dir_refused`, `test_worker_tts_has_no_egress_and_no_ports`, `test_worker_tts_secrets_minimal`, `test_tts_config_loaded_from_vendored_copy_only`. Verify: `make convert-models`, `make check-models` (new `tts` check: synthesise one fixed sentence offline), `make scan`, `make scan-images` (add `tts` to the image loop and to the CI `images` job). **Independent security review required.**
3. **1.6b-3 Watermark (always on).** AudioSeal (`facebook/audioseal` @ `3c19eba5`, MIT; `.pth` pickles converted offline; never its `weights_only=False` loader). `pipeline/audio/watermark.py`: `embed(wav: np.ndarray, sr: int) -> np.ndarray`, `detect(wav, sr) -> float`; threshold `watermark_min` (start 0.9) in `configs/thresholds.yaml`. There is **no setting to disable it**: `VoiceLine` already refuses `audio_ref` without `watermarked=True`, and the flow sets `watermarked` only after `detect >= watermark_min`. Tests: `test_embed_then_detect_above_threshold`, `test_unwatermarked_audio_below_threshold`, `test_no_config_key_disables_watermark` (settings schema has no such field; extra keys forbidden), `test_watermark_survives_aac_and_mix` (marked `models`: AAC 128 kbps via FFmpeg and a mix with -12 dB noise; record the score; the audit lists survival as unmeasured).
4. **1.6b-4 TTS provider interface.** `pipeline/audio/tts.py`: `class TtsEngine(Protocol): def synthesise(self, text: str, voice: Voice, emotion: str | None, lang: str) -> SynthResult` (`wav_path`, `sr`, `duration_ms`). Untrusted text: Unicode NFKC, control characters stripped, length cap 2000, no markup/SSML interpretation, never passed to a shell. Audio written in chunks to a temp file (I2), hashed with `sha256_stream` (1.5.1). Emotion mapping: `configs/audio.yaml` maps the closed emotion vocabulary to engine instructions; free-text emotion from SceneJSON is mapped only if it matches the vocabulary, else `null` (neutral delivery), never guessed. Tests: `test_text_normalised_and_capped`, `test_control_characters_removed`, `test_audio_streamed_to_disk_in_chunks`, `test_unknown_emotion_maps_to_neutral_null`, `test_engine_load_failure_is_user_safe`.

#### 7.4.5 Sub-milestone 1.6c: voice lines, voicing, previews, recast

1. **1.6c-1 Build voice lines.** `pipeline/audio/lines.py` `build_lines(chapter, language) -> list[VoiceLine]`: one line per `speech` or `thought` bubble (captions are narration: a `narrator` character only if the owner casts one; SFX never voiced) with a speaker link and a current casting for the language; text from OCR (`ja`) or the aligned English edition (`en`, V1); `emotion` Cited from the speaker's figure emotion in that panel's scene (panel id cited) or null. Missing speaker -> review `line_no_speaker`; missing casting -> `line_no_voice`; OCR below 0.90 -> line built but status `review` until an editor confirms the text. Tests: `test_line_needs_speaker_link`, `test_line_needs_casting_for_language`, `test_captions_and_sfx_not_voiced`, `test_emotion_cites_speaker_panel_or_null`, `test_low_ocr_line_waits_for_review`.
2. **1.6c-2 Voicing flow** `orchestration/voice_flow.py` (`chapter-voice` deployment on `worker-tts`, shared stage runner): per line: re-check rights and voice usability, synthesise, watermark, detect, store audio (`media` bucket) and the line in one transaction; cache by params (engine revision, voice params, text sha, emotion, language). **Trial gate:** `limit_lines` (1..20) measures RTF and projects the chapter; stage ends `pending`; full run needs `CONFIRM=yes`. Budget `max_minutes_per_chapter` (start 240). Tests: `test_voice_trial_projection_never_done`, `test_unwatermarked_audio_never_stored`, `test_locked_voice_refused_in_worker`, `test_rights_revoked_mid_run_stops`, `test_voice_rerun_fully_cached_loads_no_model`, `test_errors_and_logs_carry_no_line_text` (sentinel).
3. **1.6c-3 Sample previews.** `GET /api/projects/{id}/characters/{cid}/sample?language=` returns the character's representative real line (first voiced line, with its `panel_id`) and a signed audio URL; `POST /api/voices/{id}/preview` `{line_id}` synthesises (on `worker-tts` via a small `voice-preview` deployment, or reuses the cached `voice_previews` row) that same line with a library voice and returns a job id; `GET /api/voices/{id}/preview?line_id=` returns a signed URL valid at most 5 min, bound to the user (`sign_media`). Locked voices cannot be previewed. Tests: `test_preview_uses_selected_characters_real_line`, `test_preview_cached_by_params`, `test_preview_url_signed_user_bound_and_expiring`, `test_preview_of_locked_voice_refused`, `test_preview_line_from_other_project_refused`.
4. **1.6c-4 Voice swap re-voices only that character.** `recast_flow(project_id, character_id, language)` (deployment `chapter-recast`, started automatically by a casting change after confirmation in the UI): selects lines with that `character_id` and `language` whose `casting_version` is older than the current one; re-voices only those; then starts `validate_flow(scope="lines", ids=...)` (1.7) for those lines and `scope="shots"` for the shots containing them (Whisper round trip, watermark; lip sync is Phase 3); any failure -> review items. Tests: `test_recast_touches_only_that_character_and_language` (hashes of every other line unchanged), `test_recast_triggers_validation_for_affected_lines_and_shots`, `test_recast_failures_go_to_review`, `test_undo_triggers_recast_back`, `test_recast_refuses_locked_voice`.
5. **1.6c-5 Real run** `make voice-sample` (`tests/e2e/voice_sample.sh`): trial 10 lines, **STOP (owner)** with RTF and projection, then full run on an idle machine; record lines voiced, RTF p50/p95, total speech minutes, peak RSS, review items by kind in `docs/sample-chapter-source.md` "Voice results"; listen to 10 lines and record findings.

#### 7.4.6 Sub-milestone 1.6d: music direction study (text only)

Goal: a Kagebox cue-sheet vocabulary and prompt templates grounded in how anime and film scores work, without copying any work.

1. **1.6d-1 Study** `docs/plans/music-direction-study.md`, written from the engineer's own listening notes and published music-craft references (books and articles on film scoring, orchestration and mixing; cite title and author, quote nothing longer than a short phrase). Contents: cue taxonomy by mood and scene type (establishing, dialogue underscore, tension, action, reveal, comedy, grief, triumph, transition, end card); instrumentation palettes per cue type (aligned with `docs/design/adaptation-standard.md` section 5: hybrid orchestral, strings and brass, choir for climaxes, percussion, electronic textures for tension, piano or solo instruments under dialogue); tempo and key-mode ranges per type; leitmotif use per character (a short original motif per main character, reused and varied); silence and stingers (music drops before reveals; stingers on impacts); ducking under dialogue (depth and attack/release ranges); loudness targets for online delivery. **Rules for the study:** describe, never transcribe; no melodies, chord progressions of named tracks, stems, samples or MIDI from any existing work; never name a specific track or artist as a target in prompts (they go into the music `negative_prompts` list instead).
2. **1.6d-2 Vocabulary as code.** `pipeline/audio/music_vocab.py`: closed `Literal` sets `CueMood`, `CueSceneType`, `Instrument`; tempo and intensity ranges per scene type; `VOCABULARY_VERSION`. Prompt templates in `configs/prompts/music.v1.md` built only from vocabulary terms (no free text from OCR or model output). Tests: `test_vocabulary_closed_sets`, `test_prompt_template_uses_only_vocabulary_terms`, `test_prompt_never_contains_negative_listed_names` (artist/title ban list in `configs/audio.yaml` `music_negative_prompts`).
3. **1.6d-3 Owner review** of the study and vocabulary (**STOP (owner)**).

#### 7.4.7 Sub-milestone 1.6e: cue sheet from SceneJSON

`pipeline/audio/cues.py` `build_cue_sheet(shots, scenes, bible) -> CueSheet`:

- A cue spans consecutive shots with the same derived mood; mood comes from SceneJSON figure emotions and `action` mapped onto the closed `CueMood` set by a fixed table in `configs/audio.yaml` (unknown -> `neutral`, cited to the panels that support it; no evidence -> `mood.value=null` and the cue gets the scene type's default with `mood` null, not a guess).
- Scene type from the director's basis (establishing, dialogue, reaction, action, reveal) and chapter boundaries.
- Hit points at reveal shots, at the last shot, and at time-skip marks; `silence=true` for the 0.5-1.5 s before a reveal (adaptation standard: music drops before reveals); stingers on action shots with `zoom_in` impact.
- Leitmotif ids for characters present (from 1.3) in cues where they dominate the dialogue time.
- Intensity curve: low under dialogue, rising into hit points.

Tests: `test_cue_moods_cited_or_null`, `test_hit_points_on_reveals_and_end`, `test_silence_before_reveal`, `test_cues_cover_all_shots_without_overlap`, `test_leitmotif_only_for_present_characters`, `test_cue_sheet_deterministic`.

#### 7.4.8 Sub-milestone 1.6f: music provider, reference-audio rights gate, score draft

1. **1.6f-1 Music model audit (no code; STOP (owner) for V6).** `docs/plans/milestone-1-6-music-model-audit.md`, primary sources only, recording for each candidate: code licence, weights licence, **training-data statement** (what the publisher says the model was trained on, licensed or not), **output-licence statement** (who owns generated audio, any restrictions), size and format, `trust_remote_code`/pickle needs, CPU feasibility, GPU VRAM, and a **similarity-risk note** (does the publisher report memorisation tests; can it be prompted with artist names).

   | Candidate | Known so far | To verify |
   |---|---|---|
   | ACE-Step v1.5 | MIT (audit); its loader hard-codes `trust_remote_code` and ships a small pickle; D2: impractically slow on CPU; Together does not host it | training data and output-licence statements; weights size and format; whether a CPU run of a 30 s cue is possible at all in the 8 GB worker and how long it takes (one measured attempt, idle machine, time-boxed to 30 min); VRAM on the gpu profile |
   | Other open music models | not audited; licences vary and some popular ones have **non-commercial weights** (exclude those per the commercially-safe rule) | candidates found during the audit, each with the same table |

   Outcome options: (a) ACE-Step on the gpu profile only (milestone 2.0 host), cloud profile files `score_pending` (V5 a); (b) a CPU-feasible permissively licensed model if one exists (measured); (c) no generated music in Phase 1, licensed-library drafts only (1.6g).
2. **1.6f-2 [SEC] Music provider with a hard reference-audio rule.** `pipeline/audio/music.py`: `class MusicProvider(Protocol): def generate(self, cue: MusicCue, prompt: str, duration_ms: int, reference: AudioAsset | None) -> MusicResult`. `require_reference_rights(reference)` runs **before** any provider call: a reference is accepted only if it is an `AudioAsset` with `use="reference_for_conditioning"` and an acquisition record whose licence is `owned` or `licensed` (with `rights_doc_ref` stored and its sha256 verified), `CC0` or `PD` (snapshot hash verified); anything else raises `ReferenceRefused` and files `music_reference_refused`. There is no code path that accepts raw bytes, a URL or a file path as reference, and fine-tuning or style-transfer entry points do not exist in Phase 1. Tests: `test_reference_without_rights_evidence_refused`, `test_reference_with_mismatched_snapshot_hash_refused`, `test_cc_by_reference_refused_for_conditioning` (attribution-only licences are allowed as library drafts, not as conditioning input: recommendation, confirm with the owner), `test_provider_api_accepts_no_raw_audio_or_url` (signature test), `test_reference_recorded_in_provenance_components`.
3. **1.6f-3 Score draft flow** `orchestration/score_flow.py`: per cue, prompt from the template and vocabulary only, duration from the timeline, `negative_prompts` from config; output to `media`, `ScoreDraft(status="generated")` with full provenance (model, revision, licence, prompt template id, seed). In the `cloud` profile without an approved CPU model: one `ScoreDraft(status="pending")` per cue and a single `score_pending` review item per chapter. Trial gate `limit_cues` (1..3) before any full run. Tests: `test_cloud_profile_files_score_pending`, `test_score_trial_never_done`, `test_prompt_built_only_from_vocabulary`, `test_score_provenance_complete`.
4. **1.6f-4 [GPU] ACE-Step on the gpu profile** (only after V6 and milestone 2.0): pinned weights (pickle converted offline; its `trust_remote_code` loader is not used: vendor and review the minimal model code with an allowlist entry in `configs/trust_remote_code.yaml` only if unavoidable, with written justification), `CHECKS` entry, a 30 s cue smoke test. CPU fallback: (a) or (c) above.
5. **1.6f-5 Similarity check evaluation (no assumption that it works).** `docs/plans/milestone-1-6-similarity-check.md`: evaluate whether a local audio-fingerprint check is feasible, for example Chromaprint (`fpcalc`; licence to verify, expected LGPL-2.1, used unmodified as a tool), against what reference set (we hold no licensed catalogue of known tracks; an online lookup service such as AcoustID would send fingerprints outside the machine and is a new external service needing approval). Record recall limits: fingerprints detect copies of recordings, not re-performed melodies; melody similarity is the Phase 3 item (milestone 3.5). **STOP (owner)** to accept, reject or defer.
6. **1.6f-6 If approved:** `pipeline/validate/similarity.py` `too_close(audio, reference_index) -> float | None`; above threshold -> review `music_too_close`; `None` (cannot run) counts as failed for any cue intended for publication. Tests: `test_identical_recording_flagged`, `test_unrelated_audio_passes`, `test_check_unavailable_fails_for_publication`.

#### 7.4.9 Sub-milestone 1.6g: acquisition records (I3) and licensed-library fallback [SEC]

1. **1.6g-1 Import endpoint.** `POST /api/projects/{id}/audio-assets` (editor; multipart: audio file (WAV/FLAC/OGG/MP3 sniffed, max 50 MB, decoded and re-encoded to WAV by FFmpeg with argument lists, duration cap 10 min) + acquisition JSON + licence snapshot file). The server recomputes the snapshot sha256 and refuses on: no acquisition record, missing or mismatched snapshot, licence outside the allowlist (`CC0`, `PD`, `CC-BY-4.0` with a non-empty attribution, `owned`/`licensed` with `rights_doc_ref`), or a `source_url` that is not https. Refusals audited `audio_asset.refused` (reason code only). Tests: `test_asset_without_record_refused`, `test_snapshot_hash_mismatch_refused`, `test_disallowed_licence_refused` (`CC-BY-SA`, `CC-BY-NC`, `unknown`), `test_cc_by_requires_attribution`, `test_audio_reencoded_and_sniffed`, `test_ffmpeg_import_argument_list_no_shell`.
2. **1.6g-2 Library drafts and stingers.** An editor may assign a library asset to a cue (`ScoreDraft(status="library", asset_id=...)`) or as a stinger; the attribution is carried into the bundle (1.8.4). CC0/PD preferred (the reviewed library had about 221 CC-BY credits vs 47 CC0: attribution burden). Tests: `test_library_cue_attribution_reaches_bundle`, `test_library_asset_from_other_project_refused`.

#### 7.4.10 Security, risks, estimates

- **[SEC] tasks:** 1.6a-2, 1.6a-3, 1.6a-4, 1.6b-2, 1.6f-2, 1.6g-1. Risks: consent key exposure (mounted only in `api`), consent reads (audited), pickle conversion and config-as-code (offline sandbox, vendored config), new image egress (none), untrusted text into TTS (normalised, capped), uploaded audio (sniffed, re-encoded, argument-list FFmpeg), reference-audio laundering (hard gate).
- **Risks:** CosyVoice2 CPU speed and torch >= 2.6 compatibility unknown; Japanese emotion control quality; English alignment effort (V1); music generation not possible on CPU (deferral path defined); similarity checking is limited (fingerprints only).
- **Estimates (engineering sessions):** 1.6a 5, 1.6b 6 (plus the evaluation), 1.6c 6, 1.6d 3, 1.6e 2, 1.6f 5 (excluding GPU work), 1.6g 3. **Compute (unverified):** voicing 297 bubbles (fewer lines after captions/SFX), 15-25 min of speech, at RTF 2-10 -> 0.5-4 h; Whisper validation separate (1.7); score on CPU: deferred.

---

### 7.5 Milestone 1.7: Validate

**Goal.** Every voice line, shot and export gets a `ValidationReport`; failures become review items. A check that cannot run fails (the schema already enforces that `value=None` never passes).

**Checks in Phase 1:** `whisper_wer` (per voice line), `watermark` (per voice line, and again per export after the mux in 1.8), `identity` (per shot with characters), `traceability` (every artifact). `style` (Phase 2, milestone 2.5) and `lip_sync` (Phase 3, milestone 3.3) are **not included** in Phase 1 reports: including them with `value=None` would send everything to review. The report's provenance params list the checks that apply, so the omission is explicit.

**Owner decision W1 (Japanese metric).** WER is meaningless for unsegmented Japanese. Recommendation: character error rate (CER) for `ja`, WER for `en`/`ko`; both stored as `Check(name="whisper_wer", metric="cer"|"wer")`; thresholds `whisper_wer_max: 0.15` (exists in `configs/thresholds.yaml`) and a new `whisper_cer_max_ja` (start 0.15, calibrate on the trial).

**Files:** `pipeline/validate/{__init__,speech,watermark_check,identity,trace,report}.py`, `orchestration/validate_flow.py` (runs on `worker-cpu`; Whisper and SigLIP load one at a time), migration `0010_validation.py` (`validation_reports`: id, chapter_id, artifact_id, artifact_kind, verdict, params_hash, artifact JSON, created_at), API `GET /api/chapters/{id}/validation` and `GET /api/voice-lines/{id}/validation`, `tests/e2e/validate_sample.sh`, `Makefile` `validate-sample`.

**Tasks:**

1. **1.7.1 Speech round trip.** `pipeline/validate/speech.py`: `transcribe(wav_path, lang, model) -> str` with Whisper large-v3-turbo through transformers (approved, MIT, safetensors, `41f01f3f...`), CPU, eager attention, language forced to the line's language, greedy, no timestamps; audio read in chunks and resampled to 16 kHz; `normalise(text, lang)` (NFKC, punctuation and whitespace removed, case-folded for Latin scripts, kana kept as written, full-width digits folded); `error_rate(ref, hyp, lang) -> (metric, value)` with jiwer (dependency already present). Writes `VoiceLine.wer`; `status="passed"` or `"review"`. Tests: `test_cer_for_japanese_wer_for_english`, `test_normalisation_rules`, `test_transcription_failure_gives_none_and_fails`, `test_reference_text_is_the_voiced_text_not_ocr_raw` (the text actually sent to TTS), `test_no_line_text_in_logs`. Models test `tests/models/test_whisper_roundtrip.py`: transcribe one TTS output from the approved engine (generated, so no rights issue) and assert CER below 0.3.
2. **1.7.2 Watermark presence.** `watermark_check.check(wav) -> Check(name="watermark", metric="audioseal_score", threshold=watermark_min)`. Tests: `test_unwatermarked_line_fails_validation`, `test_watermarked_line_passes`.
3. **1.7.3 Identity.** SigLIP cosine between each character layer of a rendered shot (first, middle, last frame crops) and the character's approved crops (max), threshold `CharacterRef.identity_threshold` (0.90). In 2.5D the pixels are source pixels, so this is a regression guard for layer/mask bugs. Tests: `test_identity_passes_for_source_layer`, `test_identity_fails_for_other_character`, `test_no_approved_crops_gives_none_and_fails`.
4. **1.7.4 [SEC] Traceability.** `trace.check(artifact, chapter)`: every cited panel exists in this chapter's project; shot bubbles belong to the shot's panels; a voice line's bubble belongs to its panel and its `character_id` equals the bubble's speaker link; the line's `voice_id` equals the casting at `casting_version`; provenance has stage, tool, revision (for models) and licence; every score draft or library asset has a model ref or an acquisition record. Tests: `test_foreign_panel_fails`, `test_line_character_must_match_speaker_link`, `test_line_voice_must_match_casting_version`, `test_missing_provenance_fails`, `test_library_cue_without_acquisition_fails`.
5. **1.7.5 Flow, scopes, review.** `validate_flow(chapter_id, scope="all"|"lines"|"shots"|"export", ids=None)`; called by `recast_flow` for affected lines and shots; one report per artifact replacing the previous one; `verdict="review"` -> review item `validation_failed` with the failed check names only (never text). Trial gate `limit_lines` for Whisper. Tests: `test_check_that_cannot_run_counts_as_failed`, `test_recast_scope_validates_only_affected`, `test_reports_replace_previous_for_same_artifact`, `test_validation_trial_never_done`.
6. **1.7.6 Real run** `make validate-sample`; record pass rate per check and per language, Whisper seconds per audio second.

**Later (not Phase 1):** `lip_sync` offset check (threshold `lip_sync_offset_ms_max: 80`, exists in `configs/thresholds.yaml`) in milestone 3.3; `style` in 2.5; music similarity in 1.6f-6 (if approved) and 3.5.

**Estimates:** 7 sessions; Whisper turbo on CPU (unverified) about 0.3-1x real time -> 10-30 min for the chapter's lines. **Owner decides:** W1 and the thresholds after the trial.

---

### 7.6 Milestone 1.8: Assemble

**Goal.** One MP4 per chapter: shot clips in order, dialogue placed on its shots, score (or library drafts) if present, loudness-normalised with the music ducked under dialogue; plus a provenance bundle listing every model, revision, licence, every I3 attribution, the manga-ocr CC-BY-SA attribution and the source credit. Assembly refuses to export without the required credit.

**Files:** `pipeline/assemble/{__init__,timeline,mix,mux,bundle,credits}.py`, `configs/assemble.yaml`, `orchestration/assemble_flow.py`, migration `0011_exports.py` (`exports`: id, chapter_id, mp4_ref, bundle_ref, params_hash, created_by, created_at), API `POST /api/chapters/{id}/assemble`, `GET /api/chapters/{id}/exports` (signed download links), `tests/e2e/assemble_sample.sh`, `Makefile` `assemble-sample`.

**Mix targets (`configs/assemble.yaml`, confirm with the owner and the 1.6d study):** integrated loudness -16 LUFS for online delivery (alternative -23 LUFS EBU R128 broadcast profile), true peak -1.5 dBTP, loudness range recorded; dialogue bus normalised first; music bus ducked by 10-12 dB under dialogue with attack about 50 ms and release about 400 ms; music fully removed in cues marked `silence`; stingers not ducked unless they overlap a line. These are starting values from common practice, to be confirmed by listening.

**Tasks:**

1. **1.8.1 Voice-first timeline.** `timeline.build(shots, lines, audio_lengths, cues) -> Timeline`: each dialogue shot lasts max(rule min, lead-in + its lines back to back in bubble order + tail), clamped to rule max; overflow extends with a held frame and files `timing_overflow`; cues re-timed to the final shot boundaries; hit points snapped to shot starts. Tests: `test_shot_extends_to_fit_audio_with_hold`, `test_lines_in_bubble_order`, `test_overflow_reviewed`, `test_cue_boundaries_follow_shots`.
2. **1.8.2 Held frames.** Render appends identical held frames only (no new drawing). Test `test_hold_appends_identical_frames`.
3. **1.8.3 [SEC] Mix.** `mix.build_filtergraph(timeline, settings) -> list[str]` produces FFmpeg arguments: dialogue track from `adelay`-placed lines, music track from cues with `afade` in/out and silence segments, `sidechaincompress` (music keyed by dialogue) for ducking, `amix`, then two-pass `loudnorm` (first pass measures with `print_format=json`, second applies `measured_*` values). Approach cross-checked against the cinematic-skill `mix_soundtrack.py` study note (1.5.0). Every input path is our own temp path; no user string reaches the filtergraph. Tests: `test_filtergraph_ducks_music_under_each_line`, `test_silence_cues_have_no_music`, `test_loudnorm_two_pass_uses_measured_values`, `test_mix_argument_list_no_shell`, `test_mix_output_within_loudness_tolerance` (integration: measured integrated loudness within +/-1 LU of target, true peak <= target).
4. **1.8.4 Mux.** Concat demuxer list written by us, video copy where possible, AAC 192 kbps 48 kHz, `+faststart`, metadata with title and credit. Tests: `test_concat_list_contains_only_owned_paths`, `test_output_plays` (ffprobe duration equals timeline sum +/- 1 frame), `test_watermark_detected_after_mux` (models: every line segment cut from the final AAC still detects above `watermark_min`; failure -> `validation_failed` on the export).
5. **1.8.5 Provenance bundle.** `bundle.build(export) -> dict` (JSON in the `provenance` bucket, sha256 recorded): every artifact id in the MP4 with its provenance; unique models with revision and licence (from `LICENSES.md` data in `configs/models.yaml`); every I3 asset with source, licence, snapshot sha256 and attribution; the manga-ocr CC-BY-SA-4.0 notice; voices used with their source type and consent document ids (never contents); the music model or "no generated music"; the source credit. Tests: `test_bundle_lists_every_model_used`, `test_cc_by_asset_attribution_in_bundle`, `test_bundle_covers_every_shot_line_and_cue`, `test_bundle_contains_no_consent_contents_or_secrets`.
6. **1.8.6 Credits gate.** `credits.required(project)` from the rights record; for the sample: JA 「ブラックジャックによろしく」 佐藤秀峰; EN "Give My Regards to Black Jack" SHUHO SATO, and the suggested full credit in `docs/sample-chapter-source.md`; burned into an end card and written into MP4 metadata; CC-BY attributions appended to the end card. Export refused without it. Tests: `test_export_refused_without_credit`, `test_credit_text_unaltered`, `test_cc_by_attributions_on_end_card`.
7. **1.8.7 Real run** `make assemble-sample`; play the MP4; record duration, measured loudness, size. **Owner action:** report any publication to the rights holder within one month (sample terms, condition 2); V7 decides the music for any public release.

**Estimates:** 8 sessions; compute minutes. **Owner decides:** loudness targets after listening; publication.

---

### 7.7 Milestone 1.9: UI screens

**Goal.** Projects (01) with live pipeline status, Panel reader (02), Series bible (03), Voice casting (05), and a minimal Review queue list (no design file; follow the design rules). WCAG 2.2 AA, keyboard-only paths, 44 px targets, 375 px viewport.

**Owner decision (STOP before 1.9.1):** new web dependencies: `@fontsource/noto-sans-jp`, `@fontsource/noto-sans-kr` (fonts OFL-1.1), `@playwright/test`, `@axe-core/playwright` (licences to verify; expected Apache-2.0 and MPL-2.0 respectively, **unverified**), and a pinned Playwright browser image (for example `mcr.microsoft.com/playwright` by digest) for `make test-e2e-web`.

**Files:** `apps/web/src/app/projects/[id]/page.tsx` (status), `apps/web/src/app/chapters/[id]/reader/page.tsx`, `apps/web/src/app/projects/[id]/bible/page.tsx`, `apps/web/src/app/projects/[id]/casting/page.tsx`, `apps/web/src/app/projects/[id]/review/page.tsx`, components under `apps/web/src/components/` (`PanelInspector`, `CitedField`, `CropGrid`, `VoiceCard`, `Waveform`, `StatusBadge`), `apps/web/src/lib/api.ts` (typed clients), `apps/web/e2e/*.spec.ts`, `Makefile` `test-e2e-web`.

**Tasks** (each: Vitest component tests first, then Playwright + axe spec):

1. **1.9.1 Shared components:** `CitedField` shows value plus "from panel X" links or "No visual evidence" for null; `StatusBadge` blue pass / amber review with text labels (never colour alone). Tests: `CitedField.test.tsx` (`renders null as no evidence`, `lists source panels`), `StatusBadge.test.tsx` (`always has a text label`).
2. **1.9.2 Projects screen** with stage statuses from `GET /api/chapters/{id}/stages` (polling every 10 s while running), rights lock state. Spec: `projects.spec.ts` (`axe has no violations`, `keyboard reaches every action`, `375px no horizontal scroll`).
3. **1.9.3 Panel reader:** panels in reading order, inspector with every SceneJSON field cited, OCR confidence with flag below 0.90, unknown fields listed. Images through signed URLs only. Spec `reader.spec.ts`.
4. **1.9.4 Series bible:** characters, crop grid with approve/reject/merge/split, rename with evidence panels. Spec `bible.spec.ts` (`approve via keyboard`, `merge confirmation dialog is labelled`).
5. **1.9.5 Voice casting (screen 05, `docs/design/screens/05-voice-casting.dc.html`):** a row per character with its current voice for the selected language, a waveform (`Waveform` component: peaks computed server-side and returned as a small JSON array with the sample, so the browser never decodes untrusted audio for drawing), a real button "Play sample" that plays the character's real line (with its panel id shown and linked to the Panel reader); "Change voice" opens the voice library dialog where every voice previews **that same line** (1.6c-3), "Assign" (calls `PUT /castings`, shows the new version and a confirmation that only this character's lines will be re-voiced, then starts the recast), "Undo" (calls `/castings/undo`), per-language tabs (JA, EN per V1), locked voices (licensed actor or cloned without signed consent) shown disabled with the text reason "Consent not signed" (never colour alone), consent status and a link to the consent upload for owners. Recast progress from `GET /api/chapters/{id}/stages` (`recast`). Audio via signed URLs only; CSP `media-src 'self'` and the API path; no third-party players. Vitest: `VoiceCard.test.tsx` (`locked voice button disabled with reason`, `undo hidden without history`), `Waveform.test.tsx` (`renders peaks with an accessible label`). Spec `casting.spec.ts`: `assign and undo by keyboard only`, `library previews the selected characters line`, `axe has no violations`, `375px no horizontal scroll`, `play buttons are 44px targets`.
6. **1.9.6 Review queue list** (`GET /api/projects/{id}/review-items`, filter by kind, mark resolved (editor, audited, new API `POST /api/review-items/{id}/resolve`)). Spec `review.spec.ts`.
7. **1.9.7 Fonts:** self-hosted Noto Sans JP/KR; `layout.tsx` font stack per `tokens.json`. Test `fonts are served from same origin` (no external font requests in Playwright network log).

**Security:** CSRF on every write from the UI (existing `x-csrf-token` flow in `src/lib/client.ts`), no `dangerouslySetInnerHTML` for OCR text (render as text), CSP strict. **Estimates:** 12 sessions. **Owner decides:** dependencies and the browser image.

---

### 7.8 Milestone 1.10: End-to-end

**Goal.** `make e2e` turns the sample chapter into an MP4 plus provenance bundle; a CI variant runs the whole pipeline on synthetic pages with replay providers and fakes (CI never loads a model).

**Owner decision:** **E1 Sample source.** `CLAUDE.md` asks for a public-domain sample; the current sample (Black Jack vol. 1 ch. 1) is **licensed** under the rights holder's free secondary-use terms, not public domain (`docs/sample-chapter-source.md`). Its pages cannot be committed. Recommendation: keep it for the local live run; CI uses synthetic pages drawn in code; optionally add Planet Comics #41 (US public domain, pending a manual renewal check) for an LTR live run.

**Tasks:**

1. **1.10.1 Synthetic chapter generator** `tests/e2e/synthetic_chapter.py` (Pillow: 4 pages, RTL grid, balloons with drawn text, two distinct figures). Test `test_synthetic_chapter_deterministic`.
2. **1.10.2 CI pipeline test** `tests/unit/test_pipeline_e2e_replay.py`: SQLite + `MemoryObjectStore`, replay VLM, fake segmenter/embedder/TTS/whisper with deterministic outputs, real director, real compositor at 160x90, real FFmpeg (in the worker image only; mark `integration` if FFmpeg is not in the test container), asserts: every shot traces to panels, every line watermarked, bundle complete, MP4 duration equals timeline.
3. **1.10.3 Live target** `make e2e` -> `tests/e2e/e2e_sample.sh`: runs ingest (or reuses), understand, bible, director, render, voice, validate, assemble with each stage's trial and an explicit `CONFIRM=yes` for full runs; prints counts and timings only.
4. **1.10.4 Recast proof:** script step recasts one character and asserts only its lines changed (hashes) and were re-validated.
5. **1.10.5 Docs:** `README.md` (Mermaid diagram updated with all stages, setup, per-profile requirements), `docs/sample-chapter-source.md` (source and justification, results of each stage), `docs/plans/phase-1.md` status table.

**Done for Phase 1:** `make e2e` green locally; CI green; the four screens pass Playwright + axe at desktop and 375 px; recast proof; `make scan` and `make scan-images` zero high/critical. **Estimates:** 6 sessions; full live run (all stages) roughly 10-14 h of compute on the M2 (understand 5 h measured; the rest estimates).

---

### 7.9 Cross-cutting steps for Phase 1

| Area | Step per milestone | Command or file |
|---|---|---|
| CI | Keep `ruff`, `mypy --strict`, `bandit`, `pip-audit`, `pnpm audit`, `hadolint`, Trivy fs and image, gitleaks green; any new Dockerfile (`tts.Dockerfile`) is added to the `scan` hadolint lines and to `scan-images` and the CI `images` job | `Makefile`, `.github/workflows/ci.yml` |
| Licences | A `LICENSES.md` row for every model, image and direct dependency; audit entry; `configs/models.yaml` pin | `LICENSES.md`, `docs/plans/phase-1-model-audit.md` |
| Provenance | Every new artifact has `Provenance` with `components`; bundle (1.8) covers it | schemas |
| Docker hardening | New services use `x-hardening` (read-only, no-new-privileges, cap_drop ALL), non-root user, digest-pinned base, healthcheck, `mem_limit`, internal network only, only the secrets they read (extend `test_workers_get_only_the_secrets_they_read`) | `deploy/compose.yaml`, `tests/unit/test_compose.py` |
| Accessibility | Vitest + Playwright + axe for every new screen (1.9) | `apps/web` |
| Docs | README stage section and make targets; results in `docs/sample-chapter-source.md`; status in `docs/plans/phase-1.md` | |
| Release checklist | Section 10.3 | |

### 7.10 Phases 2, 3 and 4 (linked files)

| Phase | File | Milestones | Hardware |
|---|---|---|---|
| 2 Generative shots | [`handoff-phase-2.md`](handoff-phase-2.md) | 2.0 GPU host and gpu profile; 2.1 StyleProfile per series and Art direction API; 2.2 pluggable in-betweening and model evaluation; 2.3 line-art lock; 2.4 cel colour, retiming, holds, camera-only interpolation; 2.5 style guard and identity checks; 2.6 generative trial-and-budget gate; 2.7 A/B harness; 2.8 UI: Art direction, Director studio, Review queue; 2.9 Phase 2 end-to-end | Mostly **[GPU]**; CPU parts listed per task |
| 3 Polish | [`handoff-phase-3.md`](handoff-phase-3.md) | 3.0 training infrastructure; 3.1 per-character LoRAs; 3.2 series style LoRA; 3.3 lip sync; 3.4 foley; 3.5 melody-similarity check; 3.6 Phase 3 end-to-end | **[GPU]** for training and generation; CPU fallbacks listed |
| 4 Production | [`handoff-phase-4.md`](handoff-phase-4.md) | 4.1 episode assembly; 4.2 OTIO export; 4.3 performance dashboard; 4.4 cost dashboard; 4.5 production hardening; 4.6 backups and operations; 4.7 release | CPU |

---

## 8. Optional and later work

1. **Quantized Qwen3.5-9B bake-off** (milestone 1.2 Task 10, optional). Needs a new runtime (llama.cpp/GGUF with a multimodal projector, or torchao int4 inside transformers) and a GGUF `format_exception`. Step 1: vet each runtime from primary sources (licence, arm64 Linux build, Qwen3.5 vision support, no remote code, no pickle in the load path, pinned hashes, offline, memory about 5-6 GB in the 8 GB worker, seconds per panel). **STOP (owner):** approve the dependency and the format exception or not. Then: dependency locked with hashes, reviewed `format_exception` with tests that a GGUF outside an approved entry is refused, a provider behind `provider_for`, `CHECKS` entry, `LICENSES.md` and audit entries, and a run through `orchestration/understand_bakeoff.py`. A change of `stages.understand` re-records `tests/fixtures/understand/`.
2. **Periodic orphan-run reconciliation.** Call `reconcile_orphaned_runs` every 5 minutes from a background thread in `orchestration/serve.py` (same rules: never young runs, never runs owning an alive row). Tests: `test_periodic_reconcile_marks_dead_run_after_window`, `test_periodic_reconcile_never_touches_alive_run`, `test_reconcile_failure_never_stops_serving`. Also add every new deployment (bible, director, render, voice, validate, assemble) to `KINDS`.
3. **Detector improvement for SFX and signs.** Evaluate an SFX-aware text detector (candidates to audit; none approved) or an SFX label; measure recall on the 27-panel by-eye set from `docs/sample-chapter-source.md`; reduce over-masking with per-region masks (text pixels only, not the whole box) using OCR line boxes. Owner approval for any new model.
4. **Ingest:** slanted-gutter splitting, borderless panels, `panel_irregular` precision, SFX as a bubble kind.
5. **Milestone 1.2 deferred minors** (`docs/superpowers/plans/2026-11-19-milestone-1-2-understand.md`, "Deferred minors"): `_neutralise` documentation, `fixture_key` verification helper, `test_pin_models.py` style, `check_models` `enable_thinking=False`; and from the ledger: `claim_stage` hides FK failures in `except IntegrityError`, `counts=None` stores JSON null, `MAX_TRIAL_PANELS` duplicated in `routes/chapters.py` and `understand_flow.py` without an equality test, understand rights denial audited as `ingest.denied` (should be `understand.denied`), cache key lacks a contract version constant.

**Phases 2 to 4 in 15 lines** (full task lists in the linked files):

1. Phase 2 needs a GPU host (rented or owned, NVIDIA, 24 GB+ VRAM expected); the Mac stays the control plane and CPU worker.
2. 2.0 builds the `worker-gpu` CUDA image with ComfyUI in API mode on the internal network, vetted custom nodes pinned by commit.
3. 2.1 makes `StyleProfile` per series editable (Art direction API), palette from 1.5.
4. 2.2 defines an `Inbetweener` interface and evaluates ToonComposer, ToonCrafter, AniDoc (licences first); Wan 2.2 / LTX-2 only as fallback with line-art conditioning.
5. 2.3 locks line art: extract source lines, condition generation, verify per frame.
6. 2.4 quantises to flat cel colour, retimes to twos/threes with holds, interpolates only camera moves.
7. 2.5 adds the style guard and identity checks; 2.6 the generative trial-and-budget gate; 2.7 the A/B harness.
8. 2.8 builds the Art direction, Director studio and Review queue screens; 2.9 the Phase 2 end-to-end.
9. Phase 3 trains per-character and series style LoRAs (GPU), adds lip sync and foley (MMAudio, licence check first).
10. Phase 3 also adds a melody-similarity check for the score.
11. Phase 4 assembles multi-chapter episodes, exports OpenTimelineIO for DaVinci Resolve.
12. Phase 4 adds performance and cost dashboards.
13. Phase 4 closes the security follow-ups (PDF child isolation, ingest cancel), backups and operations.
14. Phase 4 ends with a release checklist and a production deployment profile.
15. Every phase keeps the same rules: rights gate, citations, consent, provenance, scans at zero high/critical.

---

## 9. Decision log and open questions

### 9.1 Owner decisions so far

| Date | Decision | Source |
|---|---|---|
| 2026-11-18 | Everything runs in Docker; nothing installed on the Mac host. | owner, recorded in the owner's assistant memory and `Makefile` header |
| 2026-11-19 | Not commercial yet, but pick licences that survive commercial use (so Magi v2 is out); routine technical choices delegated to engineering, rights/consent/new services/security still asked. | `docs/plans/phase-1-model-audit.md` "Decisions (2026-11-19)" |
| 2026-11-19 | Ingest: OpenCV panels + own reading order, NorwayFish bubble detector, tail/nearest speaker heuristic. Pickle weights converted offline; separate `worker-tts` image; ONNX exception only for CosyVoice2's two files. Whisper via transformers + safetensors. | same |
| 2026-11-19 | Sample chapter: *Give My Regards to Black Jack* vol. 1 ch. 1 (Shuho Sato), **licensed** under the rights holder's free secondary-use terms (not public domain), downloaded from densho810.com/free/ with the owner's approval; rights record basis `licensed`, evidence = terms snapshot (sha256 recorded), `allows_cloud_processing=false`. Credit and publication-report conditions apply. Planet Comics #41 kept as a public-domain fallback. | `docs/sample-chapter-source.md` |
| 2026-11-21 | Scene understanding runs **locally** (no page leaves the machine); Together candidates for understanding dropped. | audit "Decision (2026-11-21)" |
| 2026-11-22 | **Qwen3-VL-2B-Instruct approved** (`89644892...`, Apache-2.0), no fallback; rulings R3 (scene fields are visual-only; text-evidence claims nulled and reviewed), R4 (tuning pass), R5 (prompt without concrete example). | audit "Decision (2026-11-22)" |
| 2026-11-23 | R6 injection defence (masked image, no OCR text sent); R7 Qwen3.5-2B and Qwen3.5-0.8B de-approved (status proposed; their files were later deleted from the models volume to free disk); R8 `max_minutes_per_chapter` 720; R9 mask = detector on crop UNION stored page boxes. Owner approved freeing disk and `docker builder prune -af`. | audit "Decision (2026-11-23)", milestone 1.2 plan |
| 2026-11-24 | Full understand run confirmed and completed; PR #1 merged by the owner. | `docs/sample-chapter-source.md`, GitHub |
| 2026-11-19 (date as relayed by the owner) | **Local open-weight models only; Together AI is not used** for any stage. The repo still lists Together for the director (`configs/models.yaml`, `docs/plans/phase-1.md`, `LICENSES.md`): corrected by Task 1.4.0. | owner instruction relayed to the plan writer; the repo records only the 2026-11-21 local decision for understanding |
| 2026-11-24 (relayed) | **Input sources: "Manga Million" is not used as an input.** | owner instruction relayed to the plan writer; **not found anywhere in the repo** (no further context available) |
| ongoing | Model use: stronger model for plans and security reviews, cheaper model for implementation, lean review depth. | owner preference |

### 9.2 Open questions for the owner

| ID | Question | Recommendation | Needed by |
|---|---|---|---|
| D-DIR | Director engine (rules or a local LLM; no Together) and the `stages.director` config | rules now, Qwen3-VL-2B text-only as optional refinement | 1.4 |
| B1 | Character crop source | SAM 2.1 point grid + manual correction | 1.3 Task 3 |
| B2 | Dialogue/voice-profile Qdrant collections now or later | later | 1.3 |
| B3 | Character naming | editor names with evidence panels | 1.3 |
| R1 | LaMa pin vs OpenCV inpaint | OpenCV first | 1.5 |
| R2 | Output resolution/codec | 1080p, 24 fps, H.264 | 1.5 |
| Skia | `skia-python` if the study recommends it | decide on the study note | 1.5.0 |
| V1-V8 | Voice and music decisions: languages and English source, TTS engine, voice sources, consent encryption dependency, score on the Mac, music model, **human composer or licensed library for any final release**, voice designs | see the table in section 7.4.1 | 1.6 |
| W1 | CER for Japanese | yes | 1.7 |
| E1 | Licensed sample for live e2e, synthetic in CI | yes | 1.10 |
| UI1 | Web test dependencies and Playwright image | yes | 1.9 |
| G1 | GPU host for Phase 2 (rent, buy, or stop at Phase 1) | decide before Phase 2 | 2.0 |
| DOC | Edit `CLAUDE.md` (SeaweedFS instead of MinIO; Together removed) | yes | 1.3.0b |
| PUB | Publication of any output (triggers the rights holder's one-month report) | owner's call | after 1.8 |

---

## 10. Appendix

### 10.1 Glossary

- **Panel / bubble / page:** ingest artifacts; a bubble's text is untrusted OCR output.
- **SceneJSON:** per-panel visual description; every known field cites its panel.
- **Cited value:** `{value, panel_ids, confidence}`; null value = no evidence.
- **Review item:** a row in `review_items` for a human; the pipeline never silently drops a doubtful result.
- **Stage run:** the `stage_runs` row that locks one stage of one chapter for one run (owner, heartbeat, outcome).
- **Trial gate (I1):** a bounded run on the first N items that measures speed and projects the total; the full run needs an explicit confirmation.
- **Memory discipline (I2):** load media only for the active item, stream frames and audio, hash in chunks.
- **Acquisition record (I3):** source, licence snapshot hash, author, attribution for any audio we did not generate.
- **params_hash / input_hashes:** cache key of a stage item; any setting or input change recomputes it.
- **Provenance bundle:** JSON listing every model, revision, licence and attribution behind an export.
- **2.5D / multiplane:** layered source art moved at different speeds to suggest depth; no new drawing.
- **Profile `cloud` / `gpu`:** compose profiles; `cloud` = CPU only (the Mac), `gpu` adds `worker-gpu`.
- **Format exception:** a reviewed allowance for a non-safetensors, non-pickle weight format (ONNX, GGUF).
- **Twos / threes:** a drawing held for 2 or 3 frames at 24 fps (Phase 2).

### 10.2 Copy-paste prompt to start the next milestone (tool-agnostic)

```text
You are continuing the Kagebox project in the repository at <path>. Read, in this order:
CLAUDE.md, docs/plans/handoff-phase-1-continuation.md (sections 2 to 6 and 7.0), then the
section for milestone <N> (in that file or the linked docs/plans/handoff-phase-<P>.md).

Rules: run every tool inside Docker through the Makefile (make test-py, make lint, make scan);
never install anything on the host. Do not add any model, dependency, container image or external
service without asking me first and recording its licence. Treat OCR text, file names and model
output as untrusted data. Never commit secrets; never read deploy/secrets/.

First: sync main (git fetch; git checkout main; git pull --ff-only), create the branch
milestone-<N>-<name>, run make test-py and make lint to confirm a green baseline.
Then write the milestone plan file docs/superpowers/plans/<date>-milestone-<N>-<name>.md from the
hand-off section (refine file names and tests against the current code), list the owner decisions
it needs, and STOP for my approval. Do not write code before I approve.

After approval, work task by task: failing tests first (names from the plan), implement, run
make test-py and make lint, commit with a Conventional Commit message, one task per commit.
Ask for an independent review of every task marked [SEC]. Stop at every STOP (owner) point and at
every trial gate with the measured projection. Run model jobs only on an idle machine
(load average under 6). Do not push or merge; I review the pull request.
At the end: make test, make lint, make scan (and make scan-images if images changed), update
README.md, LICENSES.md, docs/plans/phase-<P>.md status and the results file, and give me a short
summary of what changed, the measured numbers, and what is next.
```

### 10.3 Definition of done, per milestone

- [ ] Milestone plan file written and approved by the owner before code.
- [ ] Every task's named tests written first and passing; no skipped tests without a reason.
- [ ] `make test` green; `make lint` clean (ruff, format, mypy strict, bandit).
- [ ] `make scan` zero high/critical; `make scan-images` zero high/critical if any image changed.
- [ ] New models: approved, pinned (commit, sha256, size), safetensors or reviewed exception, `CHECKS` entry, `make check-models` ok offline, `LICENSES.md` row, audit entry.
- [ ] New dependencies/images: approved, locked with hashes or digests, licence recorded.
- [ ] Rights gate checked in API, flow start and every per-item write; tests prove a revoked record stops the run.
- [ ] Every artifact cites panels; null when no evidence; doubtful output becomes review items; a check that cannot run fails.
- [ ] No OCR text, prompts, model output, secrets or tracebacks in logs, errors or API responses (sentinel tests).
- [ ] Compose: hardening, non-root, no published ports except Caddy, only needed secrets, no egress unless tested; `tests/unit/test_compose.py` updated.
- [ ] Long stages: lock, heartbeat, cache, trial gate, budget, hooks, deployment in `serve.py` `KINDS`.
- [ ] [SEC] tasks independently reviewed; findings fixed or recorded with the owner's acceptance.
- [ ] Real-data run (when the milestone has one): trial, owner confirmation, full run on an idle machine, results recorded in `docs/sample-chapter-source.md`.
- [ ] UI changes: Vitest, Playwright + axe at desktop and 375 px, keyboard path.
- [ ] Docs: README, `docs/plans/phase-<n>.md` status, this hand-off file corrected where reality differed.
- [ ] Whole-milestone review done; one fix wave; pull request opened for the owner (no self-merge).
