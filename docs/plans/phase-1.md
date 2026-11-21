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
| 1.5 | **2.5D render** | SAM 2.1 masks seeded from character boxes; depth order from Depth Anything V2 Small; LaMa inpaints behind characters. Numpy multiplane compositor + FFmpeg for pan / tilt / zoom / parallax. Palette extracted into StyleProfile. | Deterministic render of a fixture shot (frame hash); FFmpeg always called with argument lists, never a shell. |
| 1.6 | **Voices, casting, score** | Voice / Casting API with per-language versions, undo and audit log. Consent upload encrypted at the application level (AES-GCM, key from a Docker secret) in the `consent` bucket, every read logged. TTS per VoiceLine with the cast voice and SceneJSON emotion (CosyVoice2; Chatterbox fallback). Watermark always applied (AudioSeal) and verified. Sample previews cached, served by signed links. `recast_flow` re-voices one character's lines only. Score draft from a mood cue sheet (see D2). | Consent-locked voice refused by API *and* worker; recast touches only that character's lines; watermark detected after mux. |
| 1.7 | **Validate** | Whisper round-trip WER per line (faster-whisper, CPU int8), identity similarity vs bible per character threshold, traceability, watermark presence → ValidationReport → ReviewItems. | A check that cannot run counts as failed, never passed (already enforced by the schema). |
| 1.8 | **Assemble** | FFmpeg mux to MP4 (video, dialogue, score) plus a provenance bundle listing every model, revision and license used. OTIO export stays in Phase 4. | Output plays; bundle covers every artifact in the MP4. |
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
