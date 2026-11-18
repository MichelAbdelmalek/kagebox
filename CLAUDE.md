# Kagebox — engineering brief for Claude Code

You are the lead engineer for **Kagebox**: a pipeline and studio app that adapts comics, manga and manhwa into animated episodes while preserving the original character designs exactly, with a hand-drawn look (not CGI), cast character voices and an original score.

Treat the user as a security-conscious engineer. Proactively flag any critical or high security concern you find.

## Non-negotiable rules

1. **Rights.** Only works the user owns, has licensed, or that are public domain. Every project has a rights record; rendering is blocked server-side until it is approved.
2. **No hallucination.** Every generated field, shot, line and asset must trace to source panel IDs. Fields with no visual evidence stay null, never guessed. Anything failing a validation check goes to the review queue; nothing fails silently.
3. **Voice consent.** Voices sourced from a licensed actor or cloned from audio cannot be used unless consent is signed (enforced on the server). TTS watermarking stays on.
4. **Hand-drawn, not CGI.** Follow the art-direction rules below for every shot.
5. **Open models first.** Prefer open-weight models; Together AI is the hosted option. Record every model's name, version, license and source in `LICENSES.md` and in a per-artifact provenance manifest.
6. **Designs are the spec.** Build the UI from `docs/design/` (see its README). Tokens come from `docs/design/tokens.json`.

## Architecture

- **Language/tooling:** Python 3.11 with `uv`; TypeScript + Next.js + Tailwind for the web UI.
- **Orchestration:** Prefect flows per stage, with retries, caching and resumable runs.
- **Schemas:** strict Pydantic v2 for every inter-stage artifact: Page, Panel, Bubble, CharacterRef, SceneJSON, Shot, VoiceLine, Voice, Casting, StyleProfile, ValidationReport, RightsRecord. Structured LLM/VLM output via Pydantic AI with validation and retry.
- **Storage:** Postgres (metadata), Qdrant (series bible embeddings), MinIO (media, content-addressed, versioned).
- **Model access:** a provider interface with two backends — Together AI (API) and local (ComfyUI in API mode / Hugging Face). Model IDs and revisions pinned in `configs/models.yaml`.
- **Repo layout:** `apps/web`, `apps/api`, `pipeline/{ingest,understand,bible,director,render,audio,validate,assemble}`, `orchestration/`, `configs/`, `deploy/`, `tests/`, `docs/`.

## Pipeline stages

1. **Ingest:** CBZ / PDF / image folders; panel detection and reading order (manga RTL, comic LTR, manhwa vertical-scroll slicing); bubble detection; OCR (manga-ocr for Japanese, PaddleOCR for Korean/English); bubble-to-speaker linking. Evaluate Magi v2 for panels and speakers.
2. **Understand:** a VLM (Qwen-VL via Together) produces SceneJSON per panel; every field cites its panel ID; OCR confidence below 0.90 is flagged.
3. **Series bible (RAG):** Qdrant collections for character reference crops with SigLIP or DINOv2 embeddings, character clustering, outfits per chapter, dialogue DB, locations, voice profiles. `get_context(shot)` returns exact references for every character and location in a shot.
4. **Director:** an LLM turns panels into a Shot list (camera move, duration, transition), constrained to panel content only. Source panels are keyframes.
5. **Render:** Phase 1 = 2.5D motion comic (layer separation, multiplane parallax, camera moves) with FFmpeg. Phase 2+ = generative shots (see art direction).
6. **Audio:** TTS per VoiceLine using the character's cast voice (CosyVoice2 multilingual; Chatterbox as alternative), emotion from SceneJSON. Score drafts with ACE-Step v1.5 from a mood cue sheet. Foley with MMAudio.
7. **Validate:** Whisper round-trip of every voice line vs source text (WER threshold in config); identity similarity vs bible (threshold per character); style similarity vs source pages; lip-sync offset; traceability to panel IDs.
8. **Assemble:** FFmpeg mux to MP4 plus an OpenTimelineIO export for DaVinci Resolve.

## Voice casting

- Data: `Voice {id, name, description, source: designed|licensed_actor|cloned, consent_status, consent_doc_ref, engine, reference_audio_ref, languages[]}` and `Casting {project_id, character_id, language, voice_id, version, changed_by, changed_at}`. Casting is versioned per language track; every change is audit-logged.
- Screen (`05-voice-casting`): characters with current voice, waveform and play; samples read a real line of that character (with its panel ID). "Change voice" opens the library, where each voice previews that same line. Assign, undo, per-language casting.
- A swap enqueues re-voicing of only that character's lines, then reruns the Whisper check and lip sync for affected shots; failures go to the review queue.
- Consent documents stored encrypted in MinIO with access logging. Sample previews cached and served by signed, short-lived URLs.

## Hand-drawn art direction

- `StyleProfile` per series: `frame_timing` (ones|twos|threes|mixed, default mixed), `shading` (flat|cel2|screentone), `camera` (multiplane|free3d, default multiplane), `lock_line_art`, `smear_frames`, `interpolation_scope` (default camera_only), `negative_prompts[]`, `style_threshold` (default 0.85), `palette` extracted from the source pages.
- Character motion: source panels as keyframes; evaluate ToonComposer, ToonCrafter and AniDoc for in-betweening/colorization behind a pluggable interface (check each license). General I2V models (Wan 2.2 / LTX-2) only as fallback, always with line-art conditioning.
- Post-processing: line-art lock (extract source lines, condition generation, verify per frame); palette quantization to flat cel colour; retiming to twos/threes with held frames; no motion blur; frame interpolation on camera moves only.
- Camera: 2.5D multiplane from layer-separated panels.
- Style guard: style-embedding similarity per shot vs source pages; below threshold goes to review. Provide an A/B harness to render one shot with two StyleProfiles side by side.

## Docker (every service)

- One `docker compose` stack: `web`, `api`, `prefect-server`, `worker-cpu` (ingest, OCR, validation), `worker-gpu` (ComfyUI API mode, TTS, music), `postgres`, `qdrant`, `minio`, `caddy`.
- Profiles: `cloud` (generation via Together AI, no GPU) and `gpu` (adds `worker-gpu` via the NVIDIA Container Toolkit).
- Multi-stage builds, minimal base images pinned by digest, non-root users, read-only root filesystems where possible, `no-new-privileges`, dropped capabilities, healthchecks everywhere.
- Only `caddy` publishes ports. ComfyUI, Qdrant, Postgres and MinIO stay on internal networks; ComfyUI must never be reachable from outside.
- Secrets via Docker secrets / env files excluded from git, with `.env.example`. Never bake keys into images. Never mount the Docker socket.
- Model weights on a named volume mounted read-only into workers; a one-shot `models-fetch` job downloads pinned `.safetensors` revisions and verifies checksums.
- Makefile targets: `up-cloud`, `up-gpu`, `down`, `logs`, `test`, `scan`.

## Web UI

- Screens (from `docs/design/screens`): Projects, Panel reader, Series bible, Voice casting, Art direction, Director studio, Review queue.
- Theme from `docs/design/tokens.json`; fonts Space Grotesk, Inter, JetBrains Mono, Noto Sans JP/KR; logo from `docs/design/logo`.
- Accessibility: WCAG 2.2 AA contrast, real buttons and labels, 44 px targets, keyboard navigation, responsive down to phone width.

## Security requirements

- Load only `.safetensors` weights; pin Hugging Face revisions; never `trust_remote_code` unless whitelisted in config with a written justification.
- Validate and size-limit every upload, sniff file types, protect archive extraction against zip-slip, and treat OCR text and model output as untrusted data (never as instructions or shell input).
- Authentication on all routes, CSRF protection, strict Content-Security-Policy, signed short-lived URLs for media.
- Dependencies locked with hashes. CI runs ruff, mypy, bandit, pip-audit, npm audit, hadolint and Trivy; fail on critical or high findings.
- Vet every ComfyUI custom node before adding it, and pin it to a commit.
- Never log secrets.

## Quality

- Unit tests per stage; an end-to-end test on a small public-domain sample chapter (document its source).
- README with a Mermaid architecture diagram, setup steps, and GPU / VRAM requirements per profile.

## Phases

- **Phase 0 — Foundation:** repo layout, Docker stack (both profiles), CI with security scans, schemas, provider interface, rights records, auth.
- **Phase 1 — Motion-comic MVP for one chapter:** ingest → understand → series bible → director → 2.5D render → voices with casting → score draft → validation → assembly. UI: Projects, Panel reader, Series bible, Voice casting.
- **Phase 2 — Generative shots:** cartoon in-betweening, line-art lock, cel colour, retiming, style guard, identity checks. UI: Art direction, Director studio, Review queue.
- **Phase 3 — Polish:** per-character LoRAs, series style LoRA, lip sync, foley.
- **Phase 4 — Production:** episode assembly, OTIO export, performance and cost dashboards.

## How to work

- Always propose a plan and wait for approval before writing code for a new phase.
- Work one phase at a time; finish with passing tests and a short summary of what changed and what is next.
- Ask before adding any new external service, model or dependency, and record its license.
