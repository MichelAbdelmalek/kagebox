# Kagebox hand-off: Phase 2, generative shots

Part of the hand-off; start at [`handoff-phase-1-continuation.md`](handoff-phase-1-continuation.md) (index, rules, environment, architecture, conventions, decision log). This file is a **proposal**: nothing here is approved until the owner says so, and Phase 2 starts only after Phase 1 is done (`make e2e` green). Facts marked **(unverified)** were not checked against primary sources while writing; every model and licence below must go through the audit method of `docs/plans/phase-1-model-audit.md` before use.

## Contents

- [Phase goal and hardware](#phase-goal-and-hardware)
- [2.0 GPU host and gpu profile](#20-gpu-host-and-gpu-profile-gpu)
- [2.1 StyleProfile per series and Art direction API](#21-styleprofile-per-series-and-art-direction-api-cpu)
- [2.2 Pluggable in-betweening and model evaluation](#22-pluggable-in-betweening-and-model-evaluation-gpu)
- [2.3 Line-art lock](#23-line-art-lock-cpu--gpu)
- [2.4 Cel colour, retiming, holds, camera-only interpolation](#24-cel-colour-retiming-holds-camera-only-interpolation-cpu)
- [2.5 Style guard and identity checks](#25-style-guard-and-identity-checks-cpu)
- [2.6 Generative trial-and-budget gate](#26-generative-trial-and-budget-gate-cpu)
- [2.7 A/B harness for two StyleProfiles](#27-ab-harness-for-two-styleprofiles-gpu)
- [2.8 UI: Art direction, Director studio, Review queue](#28-ui-art-direction-director-studio-review-queue-cpu)
- [2.9 Phase 2 end-to-end](#29-phase-2-end-to-end)
- [Cross-cutting steps for Phase 2](#cross-cutting-steps-for-phase-2)
- [Owner decisions for Phase 2](#owner-decisions-for-phase-2)

## Phase goal and hardware

**Goal.** Selected shots gain real character motion: source panels are keyframes, a cartoon in-betweening model draws the frames between them, line art is locked to the source, colour is flattened to the series palette, timing is retimed to twos/threes with holds, and every generated frame passes a style guard and identity check or goes to review. Shots that do not need motion stay 2.5D (Phase 1).

**Hardware.** Items marked **[GPU]** need an NVIDIA GPU host with the NVIDIA Container Toolkit; the owner's M2 16 GB with Docker has **no usable GPU**. Expected VRAM for video in-betweening models: 24 GB or more (README estimate, **unverified per model**). **CPU-only fallback for the whole phase:** keep Phase 1 2.5D shots; build and test every CPU item (2.1, 2.3 extraction and verification, 2.4, 2.5, 2.6, 2.8) against recorded fixtures and a fake in-betweener, so the GPU work is a provider swap later. Owner decision G1 (rent, buy or stop at Phase 1) gates 2.0.

**Rules carried over.** Rights gate in API, flow start and every per-item write; every generated frame cites its keyframe panels (`Shot.source_panel_ids`); provenance with model, revision, licence, seed, StyleProfile hash; no generated art without source keyframes ("designs are the spec"); trial-before-spend with a budget cap for every generative batch.

---

## 2.0 GPU host and gpu profile [GPU]

**Goal.** A reproducible `gpu` profile: `worker-gpu` CUDA image with ComfyUI in API mode on the internal network only, vetted pinned custom nodes, its own lockfile, models from the read-only volume.

**Owner decisions:** G1 GPU host (rented cloud GPU, owned workstation, or none); G2 where the host runs (the Mac stays the control plane? or the whole stack moves to the GPU host). Recommendation: run the whole compose stack on the GPU host with `make up-gpu`, Caddy bound to `127.0.0.1` and reached through an SSH tunnel; never expose ports publicly. A rented host means source pages leave the owner's machine: the rights record must allow it (`allows_cloud_processing`) and the owner must accept the provider's terms (new external service: **approval**).

**Files:** `deploy/docker/gpu.Dockerfile` (multi-stage; CUDA runtime base pinned by digest; non-root; read-only rootfs with a tmpfs for ComfyUI temp; ComfyUI checked out at a pinned commit; custom nodes copied from `deploy/comfyui/nodes.lock.yaml` at pinned commits), `deploy/gpu/pyproject.toml` + lock (CUDA torch), `deploy/comfyui/nodes.lock.yaml` (repo, commit, licence, reviewer, date, justification per node), `deploy/comfyui/extra_model_paths.yaml` (models from `/models` read-only), `deploy/compose.yaml` (`worker-gpu` uses `kagebox/worker-gpu:dev`; ComfyUI listens on `127.0.0.1` inside the container or on an internal-only network; no `ports:`), `pipeline/providers/comfyui.py`, `tests/unit/test_comfyui_client.py`, `tests/unit/test_compose.py`, `Makefile` (`gpu-image`, `check-models-gpu`).

**Tasks:**

1. **2.0.1 [SEC] Image and compose.** Tests first: `test_worker_gpu_has_no_published_ports`, `test_comfyui_not_on_edge_or_front_network`, `test_worker_gpu_no_egress`, `test_worker_gpu_secrets_minimal`, `test_gpu_image_base_pinned_by_digest` (parse the Dockerfile). Verify: `make test-py`, `make scan` (hadolint on `gpu.Dockerfile`), on the GPU host `make up-gpu` then `docker compose ... ps` all healthy; `make scan-images` with the gpu image added.
2. **2.0.2 [SEC] Custom node vetting.** For each node: read its code at the pinned commit (network calls, `eval`/`exec`, pickle loads, downloads at runtime, file writes outside its dir), licence, maintenance; record in `nodes.lock.yaml`; a test refuses any node directory not in the lock (`test_unlisted_custom_node_refused` at image build via a check script `deploy/comfyui/verify_nodes.py`). Independent security review.
3. **2.0.3 [SEC] ComfyUI client.** `pipeline/providers/comfyui.py`: `ComfyClient(base_url)` posting workflow JSON built only from our templates in `configs/comfyui/workflows/*.json` (parameters substituted by key, never string formatting of user text), polling, downloading outputs, timeouts; refuses workflow nodes not in an allowlist. Tests (respx): `test_workflow_built_from_template_only`, `test_unknown_node_type_refused`, `test_timeout_is_user_safe_error`, `test_no_prompt_text_in_logs`.
4. **2.0.4 Model fetch on GPU.** Same `models-fetch` path; `make check-models-gpu` loads each approved GPU model and runs one tiny inference. Test: existing weights-policy tests cover new entries.
5. **2.0.5 Score on GPU (from 1.6f-4).** ACE-Step v1.5 if approved (V6), behind `MusicProvider`; the reference-audio gate (1.6f-2) unchanged.

**Risks:** ComfyUI and custom nodes are a large attack surface (remote code by design): keep internal-only, pinned, vetted, no egress. **Estimates:** 8 sessions plus host setup.

---

## 2.1 StyleProfile per series and Art direction API [CPU]

**Goal.** One editable `StyleProfile` per project (exists as a schema: `frame_timing`, `shading`, `camera`, `lock_line_art`, `smear_frames`, `interpolation_scope`, `negative_prompts`, `style_threshold`, `palette`), versioned and audited, with the palette from 1.5.8.

**Files:** migration `00xx_style_profiles_versions.py` (`style_profiles`: project_id, version, artifact, changed_by, changed_at), `apps/api/kagebox_api/routes/style.py`, `pipeline/render/style.py`.

**Tasks:**

1. **2.1.1** Versioned storage. Tests: `test_style_profile_versions_increment_and_audit`, `test_palette_hex_uppercase_enforced`, `test_negative_prompts_bounded`.
2. **2.1.2 [SEC] API.** `GET /api/projects/{id}/style` (viewer), `PUT /api/projects/{id}/style` (editor; full profile; audited `style.change`), `POST /api/projects/{id}/style/palette/extract` (editor; reruns 1.5.8 on chosen pages). Tests: `test_viewer_cannot_change_style`, `test_non_member_404`, `test_csrf_required`, `test_invalid_enum_422`.
3. **2.1.3** `params_hash` of every render and generation includes the StyleProfile hash. Test `test_style_change_invalidates_render_cache`.

**Estimates:** 3 sessions.

---

## 2.2 Pluggable in-betweening and model evaluation [GPU]

**Goal.** An `Inbetweener` interface with one or more approved backends; general image-to-video models only as fallback with line-art conditioning.

**Interface** (`pipeline/render/inbetween.py`): `class Inbetweener(Protocol): def inbetween(self, keyframes: list[Keyframe], n_frames: int, lineart: list[LineArt] | None, style: StyleProfile, seed: int) -> Iterator[Frame]`; `Keyframe` = source panel layer image + panel id; output frames streamed (I2). Backends: `FakeInbetweener` (CPU, cross-fade of line art for tests only, never shipped as output), `ComfyInbetweener` (workflow template per model).

**Tasks:**

1. **2.2.1 Interface and fake.** Tests: `test_output_frame_count`, `test_first_and_last_frames_equal_keyframes` (keyframes are kept exactly), `test_frames_cite_keyframe_panels`, `test_seed_determinism_contract` (same seed, same fake output).
2. **2.2.2 Evaluation (STOP (owner)).** `docs/plans/phase-2-model-audit.md` from primary sources for each candidate, with code licence, weights licence, training-data statement, format (safetensors?), remote code, VRAM, seconds per frame, input needs (sketch/line art/colour reference), and quality on 3 owner-approved test shots from the sample chapter:

   | Candidate | Role | Status |
   |---|---|---|
   | ToonComposer | cartoon in-betweening / colour | licence and weights **unverified** |
   | ToonCrafter | cartoon interpolation between keyframes | licence **unverified** (check weights separately from code) |
   | AniDoc | line-art colourisation with reference | licence **unverified** |
   | Wan 2.2 (image-to-video) | fallback only, with line-art conditioning | licence expected Apache-2.0, **unverified** |
   | LTX-2 | fallback only, with line-art conditioning | licence **unverified** (check for revenue or use restrictions) |

   Exclude any non-commercial or research-only weights (commercially-safe rule). Pickle weights only through `models-convert`.
3. **2.2.3** Backend for each approved model: pinned weights, ComfyUI workflow template, `CHECKS` entry, `LICENSES.md` row. Tests: `test_workflow_template_has_lineart_conditioning_for_fallback_models` (Wan/LTX templates refused without a line-art input), `test_backend_refuses_non_approved_model`.
4. **2.2.4 Shot selection.** Director marks shots `motion: none|inbetween` (editor-overridable); only `inbetween` shots go to 2.2. Test `test_only_marked_shots_generate`.

**Estimates:** 6 sessions plus GPU evaluation time (estimate 1-3 days of GPU, **unverified**).

---

## 2.3 Line-art lock [CPU + GPU]

**Goal.** Extract the source line art, condition generation on it, and verify per frame that the lines are kept.

**Tasks:**

1. **2.3.1 [CPU] Extraction** `pipeline/render/lineart.py`: `extract(panel_png) -> LineArt` (adaptive threshold on luminance plus morphology, screentone suppression by frequency filtering; no model; deterministic). Optional model-based extractor only after approval. Tests: `test_extract_keeps_ink_lines`, `test_screentone_not_treated_as_lines`, `test_deterministic`.
2. **2.3.2 [GPU] Conditioning:** pass line art to the in-betweener (2.2 templates).
3. **2.3.3 [CPU] Verification** `lineart.verify(frame, lineart_ref, tolerance_px=2) -> Score`: chamfer-distance based line precision/recall between frame line extraction and the interpolated reference at keyframes; per frame below `lineart_min` (config, start 0.85) -> review `lineart_drift`. Tests: `test_identical_lines_score_one`, `test_shifted_lines_within_tolerance_pass`, `test_missing_lines_fail`.

**Estimates:** 4 sessions.

---

## 2.4 Cel colour, retiming, holds, camera-only interpolation [CPU]

**Tasks:**

1. **2.4.1 Palette quantisation** `pipeline/render/cel.py`: map every pixel to the nearest `StyleProfile.palette` colour in Lab space (no dithering), keep line pixels black; `shading=cel2` allows one shadow tone per colour. Tests: `test_output_uses_only_palette_colours`, `test_lines_preserved`, `test_cel2_two_tones_max`.
2. **2.4.2 Retiming** `pipeline/render/timing.py`: `retime(frames, frame_timing, fps=24)`: ones, twos, threes, or mixed (action on twos, slow moves on threes, holds on reveals); holds duplicate frames exactly. Tests: `test_twos_hold_each_drawing_two_frames`, `test_threes`, `test_mixed_follows_basis`, `test_holds_are_identical_frames`.
3. **2.4.3 No motion blur:** `test_no_frame_is_a_blend_of_neighbours` (each output frame equals one generated drawing or one held frame).
4. **2.4.4 Smear frames** only when `smear_frames=true` and the shot basis is action; generated by the in-betweener, never by blur. Test `test_smear_only_on_action_shots`.
5. **2.4.5 Camera-only interpolation:** camera moves (2.5D compositor) may run on ones; drawn content follows `frame_timing`; `interpolation_scope=camera_only` refuses any frame-interpolation model on drawings. Classical optical flow (OpenCV DIS, no weights) is the only interpolation allowed for camera layers if needed; an interpolation model (for example RIFE; licence **unverified**) needs approval. Tests: `test_drawings_never_interpolated_in_camera_only_scope`, `test_camera_layer_motion_smooth`.

**Estimates:** 5 sessions.

---

## 2.5 Style guard and identity checks [CPU]

**Tasks:**

1. **2.5.1 Style embedding.** Choose a style embedding (SigLIP image embeddings of the frame vs source page crops is the zero-new-model baseline; a dedicated style model needs approval). `pipeline/validate/style.py` `style_similarity(frames, source_pages) -> float` (mean cosine to the nearest source crops). Calibrate `style_threshold` (default 0.85) on Phase 1 2.5D shots (which should pass) vs deliberately CGI-looking fixtures (which should fail). Tests: `test_source_frames_pass`, `test_cgi_like_fixture_fails` (fixture generated in code: smooth gradients, specular highlights), `test_check_cannot_run_fails`.
2. **2.5.2 Identity per frame:** extend 1.7.3 to generated frames: character regions (SAM box prompts from keyframe boxes tracked by the in-betweener's layer or by IoU tracking) vs bible approved crops, per-character threshold. Tests: `test_identity_drift_flagged_per_frame`, `test_unknown_character_region_fails_not_guessed`.
3. **2.5.3 Review routing:** shots below threshold -> review `style_below_threshold` / `identity_below_threshold`, shot falls back to its 2.5D render until approved. Test `test_failed_generated_shot_falls_back_to_2p5d`.

**Estimates:** 5 sessions.

---

## 2.6 Generative trial-and-budget gate [CPU]

From `docs/plans/phase-1.md` "Phase 2 carry-over".

**Tasks:**

1. **2.6.1** `generate_flow` trial: render 1-3 representative shots at the lowest resolution, store style and identity scores, measured GPU seconds per output second, projected total and cost.
2. **2.6.2 [SEC] Budget cap:** `POST /api/chapters/{id}/generate` with `{resolution, max_gpu_minutes}` accepted only after a current trial; the cap includes retries and the trial spend; the flow stops before exceeding it (checks before each shot using the measured rate); a scope or budget change needs a new confirmation. Tests: `test_full_generation_refused_without_trial`, `test_run_stops_before_cap`, `test_retry_counted_in_budget`, `test_scope_change_needs_new_confirmation`.

**Estimates:** 3 sessions.

---

## 2.7 A/B harness for two StyleProfiles [GPU]

**Tasks:**

1. **2.7.1** `orchestration/style_ab.py`: render one shot with profile A and B (same seed, same keyframes), side-by-side video and a JSON of scores; profiles are versions or drafts, not applied to the project until chosen. Tests: `test_ab_uses_same_seed_and_keyframes`, `test_ab_does_not_change_project_profile`.
2. **2.7.2** API `POST /api/shots/{id}/style-ab` (editor, behind the 2.6 budget gate), result signed URLs. Test `test_ab_counts_against_budget`.

**Estimates:** 3 sessions.

---

## 2.8 UI: Art direction, Director studio, Review queue [CPU]

Designs: `docs/design/screens/06-art-direction.dc.html`, `04-director-studio.dc.html`; Review queue follows the design rules (no file exists).

**Tasks** (Vitest first, then Playwright + axe at desktop and 375 px, keyboard-only paths):

1. **2.8.1 Art direction:** source vs CGI-to-avoid vs hand-drawn target, controls for every StyleProfile field, A/B trigger, style guard score display. Specs: `art-direction.spec.ts` (`every control has a label`, `change is audited and versioned`).
2. **2.8.2 Director studio:** shot list, source keyframe next to generated take, takes history, validation checks, accept / regenerate (budget-gated) / send to review, multi-track timeline (video, dialogue, music). Specs: `director-studio.spec.ts` (`regenerate disabled without budget`, `timeline is keyboard navigable`).
3. **2.8.3 Review queue (full):** filters by kind and stage, detail with the artifact and its source panels, resolve/reopen with audit, bulk actions. Spec `review-queue.spec.ts`.

**Estimates:** 10 sessions.

---

## 2.9 Phase 2 end-to-end

1. CI: the full pipeline with `FakeInbetweener` and replay fixtures (no GPU in CI).
2. GPU host: `make e2e-gpu` on 5 owner-chosen shots of the sample chapter with the trial gate, then a confirmed batch within a budget.
3. Done when: generated shots pass style and identity checks or are in review with a 2.5D fallback; scans clean; the three screens pass axe.

---

## Cross-cutting steps for Phase 2

- **CI:** add `gpu.Dockerfile` to hadolint and the image scan; keep pip-audit for the GPU lock; no GPU jobs in CI.
- **Licences/provenance:** `docs/plans/phase-2-model-audit.md`; `LICENSES.md` rows for every model, ComfyUI, each custom node; provenance includes seed, workflow template sha256 and StyleProfile version.
- **Docker hardening:** same rules; ComfyUI never reachable from outside; no Docker socket.
- **Accessibility:** WCAG 2.2 AA on the new screens.
- **Docs:** README GPU/VRAM table per model (measured), `docs/plans/phase-2.md` status.
- **E2E on the sample:** licensed sample live on the GPU host only with `allows_cloud_processing` if the host is rented.
- **Release checklist:** definition of done in the index file, section 10.3.

## Owner decisions for Phase 2

G1 GPU host; G2 stack placement and network exposure; in-betweening model choice after 2.2.2; style embedding choice; interpolation model (if any); budget limits per chapter; whether rented GPUs may process the licensed sample.
