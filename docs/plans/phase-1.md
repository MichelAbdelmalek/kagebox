# Phase 1 plan — motion-comic MVP for one chapter

**Goal:** one public-domain chapter goes from upload to an MP4 with cast voices and a draft score. Every shot, line and asset traces to source panel IDs, and failures land in a review queue. Default compute is the `cloud` profile on your Mac: CPU workers in Docker, VLM/LLM on Together AI.

**Done means:** `make e2e` turns the sample chapter into an MP4 plus a provenance bundle. The four Phase 1 screens pass Playwright + axe at desktop and 375 px. A recast re-voices only that character's lines and re-checks them. `make scan` and `make scan-images` stay at zero high/critical findings.

Built on Phase 0: the schemas, rights gate, provider interface, upload validation, Prefect skeleton and web shell carry over unchanged. Each stage replaces one `not_implemented` stub in `orchestration/flows.py`.

## Milestones (one reviewable PR each, in order)

| # | Milestone | Main pieces | Proof it works |
|---|---|---|---|
| 1.0 | **Models and licenses** | Verify every candidate's license and weights format, pin revisions + sha256 in `configs/models.yaml`, implement `models-fetch` (download → checksum → read-only volume), add rows to `LICENSES.md`. Worker image gains FFmpeg (LGPL build) and the CPU inference stack. | Fetch is idempotent; a tampered or `.bin` file is refused; worker image still scans clean. |
| 1.1 | **Ingest** | `POST /chapters/{id}/upload` (CBZ / PDF / images, via Phase 0 validators). Pages normalized and stored by content hash. Panel detection: OpenCV gutter detector as baseline, Magi v2 if approved (D3). Reading order for RTL / LTR, vertical slicing for manhwa. Bubble detection, OCR (manga-ocr JA; PaddleOCR or RapidOCR KO/EN). OCR below 0.90 is flagged. **Speaker linking moved to milestone 1.3** (it needs the series bible's character detections); until then `Bubble.speaker` stays null (no evidence). **Manhwa PDFs are not supported yet**: manhwa sources must be images or CBZ (a PDF is refused before rendering). Large sources are imported server-side from `data/imports/<project_id>/` (scoped per project). | Golden tests on synthetic pages for all three formats; zip-slip / bomb / PDF fixtures rejected. |
| 1.2 | **Understand** | SceneJSON per panel from a local vision model on `worker-cpu` (bf16 on CPU), with schema-validated output and retries. Every field cites its panel or stays null. Bad output after retries becomes a ReviewItem. No page leaves the machine. | Recorded-fixture contract tests; prompt-injection fixture in OCR text does not change output fields. |
| 1.3 | **Series bible** | Character crops → SigLIP embeddings (CPU) → HDBSCAN clusters → Qdrant collections (characters, locations, dialogue, voice profiles). Approve / reject crops, merge / split clusters. `get_context(shot)` returns exact refs. Speaker linking (bubble → character, cited by panel) lands here, from milestone 1.1. | Clustering on fixture crops; `get_context` never returns refs from another project. |
| 1.4 | **Director** | Together LLM turns panels into Shots (camera move, duration, transition). A validator rejects any shot or line not traceable to panels in the scene. Source panels are keyframes. | Property test: every Shot has ≥ 1 source panel from its scene; untraceable output → review. |
| 1.5 | **2.5D render** | SAM 2.1 masks seeded from character boxes; depth order from Depth Anything V2 Small; LaMa inpaints behind characters. Numpy multiplane compositor + FFmpeg for pan / tilt / zoom / parallax. Palette extracted into StyleProfile. Memory rules I2 (below). Compositor confirmed by the cinematic-skill study (below) before Task 1. | Deterministic render of a fixture shot (frame hash); FFmpeg always called with argument lists, never a shell; I2 peak-memory test. |
| 1.6 | **Voices, casting, score** | Voice / Casting API with per-language versions, undo and audit log. Consent upload encrypted at the application level (AES-GCM, key from a Docker secret) in the `consent` bucket, every read logged. TTS per VoiceLine with the cast voice and SceneJSON emotion (CosyVoice2; Chatterbox fallback). Watermark always applied (AudioSeal) and verified. Sample previews cached, served by signed links. `recast_flow` re-voices one character's lines only. Score draft from a mood cue sheet (see D2). Any non-generated audio needs an I3 acquisition record. | Consent-locked voice refused by API *and* worker; recast touches only that character's lines; watermark detected after mux; I3 audio without an acquisition record or with a disallowed licence is refused. |
| 1.7 | **Validate** | Whisper round-trip WER per line (faster-whisper, CPU int8), identity similarity vs bible per character threshold, traceability, watermark presence → ValidationReport → ReviewItems. | A check that cannot run counts as failed, never passed (already enforced by the schema). |
| 1.8 | **Assemble** | FFmpeg mux to MP4 (video, dialogue, score) plus a provenance bundle listing every model, revision and license used, and every I3 attribution. OTIO export stays in Phase 4. Loudness/ducking approach informed by the same study (`mix_soundtrack.py`). | Output plays; bundle covers every artifact in the MP4, including every CC-BY attribution. |
| 1.9 | **UI** | Projects with live pipeline status; **Panel reader** (02); **Series bible** (03); **Voice casting** (05); a minimal Review queue list (no design file exists). Noto Sans JP/KR self-hosted for CJK text. | Vitest per component; Playwright + axe (WCAG 2.2 AA), keyboard-only paths, 375 px viewport. |
| 1.10 | **End-to-end** | Sample chapter run with Together mocked by recorded fixtures in CI, plus an opt-in live run. Source and public-domain justification in `docs/sample-chapter-source.md`. | `make e2e` green. |

## Models proposed for Phase 1

These are candidates. Milestone 1.0 verifies each license and format before anything is added, and per the brief **each needs your approval**.

| Stage | Candidate | Runs on (cloud profile) | Expected license (to verify) |
|---|---|---|---|
| Panels, text boxes, speakers | Magi v2 | worker-cpu | verify; needs `trust_remote_code` (D3) |
| Panels (baseline) | OpenCV gutter detection | worker-cpu | Apache-2.0 (OpenCV) |
| OCR Japanese | manga-ocr | worker-cpu | Apache-2.0 |
| OCR Korean / English | PaddleOCR, or RapidOCR (ONNX) if Paddle has no arm64 Linux wheels | worker-cpu | Apache-2.0 |
| Scene understanding | A small local Qwen vision model (candidates: Qwen3-VL-2B-Instruct, Qwen3.5-2B, Qwen3.5-0.8B; chosen by a bake-off in 1.2) | Local (`worker-cpu`) | Apache-2.0 |
| Director | Qwen3 or gpt-oss-120b on Together | Together | Apache-2.0 |
| Embeddings | SigLIP so400m | worker-cpu | Apache-2.0 |
| Masks | SAM 2.1 (small or base) | worker-cpu | Apache-2.0 |
| Depth | Depth Anything V2 **Small** only (larger sizes are non-commercial) | worker-cpu | Apache-2.0 |
| Inpainting | LaMa | worker-cpu | Apache-2.0 |
| TTS | CosyVoice2-0.5B; Chatterbox fallback | worker-cpu (slow but workable for one chapter) | Apache-2.0 / MIT |
| Watermark | AudioSeal | worker-cpu | verify (MIT expected) |
| Speech check | Whisper large-v3-turbo via faster-whisper | worker-cpu | MIT |
| Score draft | ACE-Step v1.5 | see D2 | verify (Apache-2.0 expected) |

**New dependencies to approve** (all permissive, to be confirmed in 1.0): opencv-python-headless, numpy, onnxruntime, torch (CPU build) + transformers + safetensors, manga-ocr, paddleocr or rapidocr, qdrant-client, scikit-learn, faster-whisper, jiwer, cryptography (consent encryption), soundfile; web: `@fontsource/noto-sans-jp`, `@fontsource/noto-sans-kr`, `@playwright/test`, `@axe-core/playwright`.

## Decisions I need from you

- **D1 Sample chapter.** Which public-domain chapter? My default: a Golden Age comic from the Digital Comic Museum with documented public-domain status (LTR). RTL and vertical formats are covered by synthetic fixtures.
- **D2 Score draft on a Mac.** ACE-Step is impractically slow on CPU, Docker on macOS can't use the Apple GPU, and Together doesn't host it. Options: (a) skip the score in the `cloud` profile and file a "score pending" review item, generating it only in the `gpu` profile; (b) a native macOS worker using the Apple GPU, which breaks your "everything in Docker" rule; (c) a rented GPU host running the `gpu` profile. **My default: (a).**
- **D3 Magi v2.** It needs `trust_remote_code`. Vendor its model code into the repo at a reviewed commit and allowlist it, or use the OpenCV baseline only? **My default: vendor + review + allowlist**, with OpenCV as the fallback.
- **D4 Together.** Your API key goes in `deploy/secrets/together_api_key.txt`. Confirm your account's data-retention setting, and that licensed (non-public-domain) works may go to Together when their rights record allows it.
- **D5 Dub languages in Phase 1.** **Default: Japanese + English.** CosyVoice2's Arabic support is unverified; Korean can follow.
- **D6 Consent encryption.** **Default: application-level AES-GCM** with a key in Docker secrets, rather than relying on the object store's server-side encryption.

## Risks

- **CPU time and memory on the Mac.** SAM, LaMa, SigLIP, TTS and Whisper all run on CPU in Docker. `worker-cpu` needs about 8 GB, so Docker Desktop's memory limit may need raising. A chapter will take a while; runs are resumable and cached.
- **arm64 wheels.** Some packages (notably Paddle) may lack Linux arm64 builds. RapidOCR (ONNX) is the fallback.
- **2.5D quality** on dense pages. Mask and inpaint artefacts go to review, with manual mask correction as a later step.
- **Licenses** may rule out a candidate; each stage has a fallback.
- **Together catalogue drift.** Model IDs are pinned, recorded fixtures keep CI independent of the catalogue, and each stage has a fallback.
- **Scope.** Ten PRs; I'll stop for review after each and report test and scan results.

## Known security follow-ups

Accepted for milestone 1.1 (single operator, local stack); to be fixed before any shared deployment.

- **PDF render child isolation.** Untrusted PDFs are rendered in a child process of `worker-cpu` (`pipeline/ingest/sources.py`) limited by rlimits, a scrubbed environment and a new session only. It runs as the same uid in the same container, so a pdfium memory-safety exploit could read every secret mounted into `worker-cpu` (the database password and the object-store keys) and reach Postgres, MinIO and the internet through the `back` and `egress` networks. Milestone 1.1 mounts only the secrets worker code reads (the media signing key, session secret and Together key were removed; `tests/unit/test_compose.py` enforces it). Follow-up: render PDFs in a dedicated container (or `bwrap`/`nsjail` with `--unshare-net`) on an internal-only network, under its own uid, with no secrets and a minimal read-only bind set, and consider moving ingest off the egress-attached worker. Cost if left: a pdfium RCE could read the DB password and object-store keys.
- **Cancelling an ingest run.** `chapter-ingest` has a 3 h `timeout_seconds` and runs heartbeat the chapter, so a hung or dead run is ended or taken over; there is still no product route to cancel a queued or running ingest (the Prefect UI is not published). Follow-up: an owner-only `POST /chapters/{id}/ingest/cancel` that sets the owning flow run to Cancelling.

## Ideas adopted from stickman-video-director (reviewed 2026-11-21)

Read-only review of the public repo `kaomei/stickman-video-director`; nothing from it is run, installed or copied.

**I1 — Trial before spend.**
- *Take:* staged approval with a bounded, cheap trial and a projected total before any long or paid run.
- *Why:* the local `understand` run takes hours on CPU, and Phase 2 generative shots cost GPU time or Together credits; a wrong setting should cost minutes, not a night.
- *Lands:* milestone 1.2 (Task 6 `limit_panels`, Task 9 confirmation in `make understand-sample`) and Phase 2 (carry-over below).
- *Acceptance:* a trial on the first N panels records measured seconds/panel and a projected total in the stage run, never marks the stage `done`, and the full run starts only after an explicit confirmation.
- *Not adopted:* browser automation of a logged-in account, closed video services as defaults.
- *Source:* `skills/directing-stickman-videos/references/mode-execution-boundaries.md#a-unified-workflow` (storyboard, prompts, representative low-resolution test, production settings with a total budget cap, continuous production); CHANGELOG 2026-11-17.

**I2 — Memory discipline for local rendering.**
- *Take:* load artwork only for the active shot; write generated audio in chunks; hash large media in chunks; stream media checks instead of reading whole files.
- *Why:* `worker-cpu` has 8 GB (`mem_limit: 8g`) and SAM, LaMa and depth models already compete for it.
- *Lands:* milestone 1.5 (binding constraints), and every place we hash large media (ingest, render, audio, assembly).
- *Acceptance:* a 1.5 test renders a fixture shot of at least 60 s in a child process and asserts peak RSS stays under a configured ceiling below 8 GB and does not grow with shot length; a test hashes a file larger than the read chunk and checks the result equals the whole-file sha256 while reading at most one chunk at a time.
- *Source:* CHANGELOG entry 2026-11-18, "Code Engine rendering performance".

**I3 — Per-asset provenance for non-generated audio.**
- *Take:* every sound we did not generate carries an acquisition record: source URL, licence text snapshot with its sha256, author, attribution string, acquisition date. Licence allowlist: CC0 / public domain preferred; CC-BY only with its attribution carried into the provenance bundle; anything else refused.
- *Why:* rule 5 provenance already covers models; this extends it to audio assets. The reviewed library shows the attribution burden (about 221 CC-BY credits vs 47 CC0), so CC0 is the default choice.
- *Lands:* milestones 1.6 (voices, score) and 1.8 (assembly bundle lists every attribution); Phase 3 foley.
- *Acceptance:* importing an asset with no acquisition record, a missing or mismatched licence-snapshot hash, or a licence outside the allowlist is refused by the server; an assembled MP4 using a CC-BY asset has its attribution string in the bundle.
- *Not adopted:* prompt-only enforcement (the server enforces the allowlist).
- *Source:* `skills/directing-stickman-videos/assets/b-audio-library/` (`CREDITS.md`, `catalog.json`, `evidence/<source>/acquisition.json` with saved source pages), `references/music-sourcing-and-license.md`, `references/b-audio-library.md`.

**Not adopted at all:** generating new art (designs are the spec; source panels are keyframes), closed video services as defaults, browser automation of a logged-in account, prompt-only enforcement of rules.

### Phase 2 carry-over: trial-before-spend gate

Before a batch of generative shots: render one to three representative shots at the lowest resolution, show them with their style and identity scores, then confirm final resolution and a maximum total budget (GPU minutes or Together credits, retries included, trial spend stated). The run stops before exceeding the cap, and a scope or budget change needs a new confirmation. Enforced server-side, recorded in the stage run.

## Candidate to study at milestone 1.5: Moh4696/cinematic-skill (reviewed 2026-11-21)

Read-only look at the public repo through `gh api`; nothing from it is run, installed or copied. MIT agent skill (872 stars, 135 forks, created and last pushed 2026-11-09, 3 commits, one maintainer) that builds narrated films entirely from code: items on one board, one continuous camera move, Skia (`skia-python`) drawing, word-timed hits from the narration, synthesised sound effects (`scripts/build_sfx.py`), a ducked, loudness-normalised mix (`scripts/mix_soundtrack.py`), memory-aware parallel render (`scripts/render.py`), stills and contact strips for self-review, and a Wikimedia fetcher with credits (`scripts/fetch_commons.py`). Paths are under `skills/cinematic-film/`.

**Decision:** not adopted as a dependency, not installed, no code vendored. At milestone 1.5 planning, one Opus read-only study of `scripts/engine.py` (camera, items, timing API), `render.py`, `build_sfx.py` and `mix_soundtrack.py`, then a Skia-vs-numpy compositor comparison before 1.5 starts.

**Study checklist:**
- Camera model and keyframing, against our multiplane pan / tilt / zoom / parallax over layer-separated panels.
- How word-level timing drives events, compared with our voice-first re-timing for recast: the Director sets duration constraints, Assemble fits shots to the actual audio.
- Determinism guarantees (same input, same frame hashes).
- Peak memory per shot against the 8 GB `worker-cpu` rule (I2).
- `skia-python`: Linux arm64 wheels, wheel size, licence, loads offline. A new native dependency needs owner approval and a `LICENSES.md` row.
- Test coverage and maintenance signals at study time (today it is one day old; its only test is `tests/check.mjs`).
- Installer and supply-chain risk: its `npx skills add` and `curl | bash` installers are not used.

**Cautions:**
- Its image fetcher accepts CC BY-SA and has an `--allow-nonfree` bypass. Our allowlist stays stricter: owned, licensed or public domain (CLAUDE.md rule 1) and the I3 audio allowlist.
- It composes photos and cards; it does not preserve source art or trace output to panels, so nothing in it replaces rule 2 traceability.
- Its synthesised SFX are a candidate licence-clean baseline to compare against MMAudio for Phase 3 foley, not a replacement decision.

**Acceptance for the study:** a one-page decision note in `docs/plans/` choosing the compositor, with measured evidence on a fixture shot (frame determinism, peak RSS, seconds per frame on the M2 CPU in Docker), merged before Task 1 of milestone 1.5 begins.

## Understand: prompt-injection mitigation and its remaining risk (milestone 1.2)

The vision model never receives OCR text, and every detected text region of a panel (bubble detector run on the crop, plus every stored region of the page that touches it) is blanked with a flat colour before the model sees the image. Scene fields are visual-only. This is a mitigation, not a proof. Remaining risk: text the detector misses (low contrast, small, rotated or unusual lettering), text drawn as part of the scenery (signs, posters, sound effects in the artwork) and lettering in scripts the detector was not trained on can still be read. The impact is limited to a wrong scene description: the model has no tools and no privileges, its output is validated by a strict schema and cited by code, and any claim backed by text evidence is refused.
