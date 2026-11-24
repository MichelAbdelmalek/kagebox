# Kagebox hand-off: Phase 3, polish

Part of the hand-off; start at [`handoff-phase-1-continuation.md`](handoff-phase-1-continuation.md). A **proposal**; Phase 3 starts after Phase 2 is done. Facts marked **(unverified)** need the audit method of `docs/plans/phase-1-model-audit.md` before use.

## Contents

- [Phase goal and hardware](#phase-goal-and-hardware)
- [3.0 Training infrastructure](#30-training-infrastructure-gpu)
- [3.1 Per-character LoRAs](#31-per-character-loras-gpu)
- [3.2 Series style LoRA](#32-series-style-lora-gpu)
- [3.3 Lip sync](#33-lip-sync-cpu-first)
- [3.4 Foley](#34-foley-gpu-with-cpu-fallback)
- [3.5 Melody-similarity check](#35-melody-similarity-check-cpu)
- [3.6 Phase 3 end-to-end](#36-phase-3-end-to-end)
- [Cross-cutting steps and owner decisions](#cross-cutting-steps-and-owner-decisions)

## Phase goal and hardware

**Goal.** Characters stay on-model across generated frames (per-character LoRAs), the series look is consistent (style LoRA), mouths move with the voice (lip sync), and sound effects follow the lettered SFX on the page (foley). Training and generative audio are **[GPU]**; lip sync by mouth-shape swapping and the checks run on CPU.

**Rules.** LoRAs are trained only on crops from the project's own approved bible (rights gate covers training data; never other works). Training data, base model, hyperparameters and resulting weights are pinned and listed in provenance. Generated audio stays watermarked where it is speech; foley and music follow the I3 and reference-audio rules from milestone 1.6.

---

## 3.0 Training infrastructure [GPU]

**Files:** `pipeline/train/{__init__,dataset,lora,registry}.py`, `orchestration/train_flow.py`, `configs/train.yaml`, compose one-shot service `trainer` (gpu profile, no egress, models read-only, outputs to a `lora-staging` volume, promoted by hash like `models-promote`).

**Tasks:**

1. **3.0.1 [SEC] Dataset builder** from approved bible crops of one project: `build_dataset(project_id, character_id) -> DatasetManifest` (crop refs, sha256, panel ids, captions built from vocabulary only, never OCR text). Tests: `test_dataset_only_approved_crops_of_this_project`, `test_dataset_manifest_hashes_every_image`, `test_rights_revoked_refuses_training`.
2. **3.0.2 Trainer choice (STOP (owner)).** Evaluate trainer code (licence, safetensors output, no remote code, offline) and the base model of the chosen in-betweener/generator; record in `docs/plans/phase-3-model-audit.md`.
3. **3.0.3 Output pinning.** LoRA weights saved as safetensors, hashed, registered in a `loras` table (project, character, base model revision, dataset hash, params hash, status proposed/approved); promotion only after owner approval. Tests: `test_lora_saved_as_safetensors_only`, `test_unapproved_lora_not_loadable`.

**Estimates:** 6 sessions plus GPU hours (unverified).

## 3.1 Per-character LoRAs [GPU]

1. **3.1.1** Train one LoRA per main character (trial: one character, small step count; budget gate from 2.6). Tests (CPU, fake trainer): `test_training_trial_projection_never_done`, `test_budget_cap_respected`.
2. **3.1.2** Use in generation through the `Inbetweener` backend (LoRA path passed only from the `loras` table). Test `test_only_approved_loras_of_this_project_loaded`.
3. **3.1.3** Evaluation: identity similarity before/after on 10 shots; accept only if identity improves and style does not drop below threshold (report in `docs/plans/phase-3-results.md`).

**Estimates:** 5 sessions.

## 3.2 Series style LoRA [GPU]

1. **3.2.1** Dataset: background and full-panel crops of the project (text-masked with the 1.2 mask), balanced across chapters. Test `test_style_dataset_text_masked`.
2. **3.2.2** Train, evaluate with the style guard (2.5) against source pages; A/B harness (2.7) with and without the LoRA.
3. **3.2.3** Register and approve as in 3.0.3.

**Estimates:** 4 sessions.

## 3.3 Lip sync [CPU first]

**Owner decision L1:** approach. (a) **Mouth-flap swapping (CPU):** per character, an editor picks 3-5 mouth shapes (closed, half, open, wide, "u") from the character's own source panels in the bible; phoneme or energy timing from the voice line drives which shape is shown on twos/threes; no generative model; fits the hand-drawn look. (b) A generative lip-sync model (candidates such as LatentSync or MuseTalk; licences **unverified**; mostly trained on live-action faces, a poor fit for drawn characters; **[GPU]**). Recommendation: (a).

**Tasks (option a):**

1. **3.3.1** Mouth shape set per character (bible API: `POST /api/characters/{id}/mouths` with panel-cited crops; editor). Tests: `test_mouth_shapes_cite_source_panels`, `test_mouth_crop_from_other_project_refused`.
2. **3.3.2** Timing: `pipeline/audio/visemes.py` `mouth_track(wav, lang, fps=24, timing="twos") -> list[shape]` from short-time energy and, for Japanese, kana-to-vowel mapping of the voiced text with Whisper word timestamps (Whisper already approved). Tests: `test_silence_maps_to_closed`, `test_track_on_twos`, `test_vowel_mapping_ja`.
3. **3.3.3** Compositing: mouth region of the character layer replaced by the selected shape (aligned by an editor-set anchor box), only in dialogue shots of that speaker. Tests: `test_only_speaker_mouth_changes`, `test_off_screen_speaker_no_flap`.
4. **3.3.4** Check `lip_sync`: offset between voice onset and first open mouth frame; threshold `lip_sync_offset_ms_max: 80` (exists in `configs/thresholds.yaml`); added to `ValidationReport` from now on. Tests: `test_lip_sync_offset_within_threshold_passes`, `test_offset_over_threshold_reviewed`.

**Estimates:** 7 sessions, CPU.

## 3.4 Foley [GPU with CPU fallback]

**Owner decision F1:** foley source. (a) MMAudio (video-to-audio; code and weights licences **unverified**: check separately, exclude non-commercial weights; training data statement; **[GPU]**); (b) **CPU fallback:** synthesised SFX in code (noise, envelopes, filters; the cinematic-skill `build_sfx.py` approach noted in `docs/plans/phase-1.md`, studied, not copied) plus a CC0 library through I3 records; (c) both. Recommendation: (b) first, (a) after audit.

**Tasks:**

1. **3.4.1** SFX events from the page: SFX lettering detections (needs the SFX detector improvement, index section 8) or editor-marked SFX regions per panel, cited by panel; mapped to a closed `SfxKind` vocabulary. Tests: `test_sfx_events_cite_panels`, `test_no_sfx_without_evidence`.
2. **3.4.2** `FoleyProvider` protocol with `SynthFoley` (CPU) and later `MmaudioFoley` (GPU); the reference-audio rule from 1.6f-2 applies to any conditioning audio. Tests: `test_synth_foley_deterministic`, `test_foley_reference_without_rights_refused`.
3. **3.4.3** Mix: foley bus in 1.8 mix, ducked less than music under dialogue; loudness targets unchanged. Test `test_foley_bus_in_mix`.
4. **3.4.4** Provenance: every foley clip has a model ref or an acquisition record; bundle lists them.

**Estimates:** 6 sessions.

## 3.5 Melody-similarity check [CPU]

From `docs/design/adaptation-standard.md` section 5 ("A melody-similarity check against known themes comes later").

1. **3.5.1 Evaluation (STOP (owner)).** Feasible approaches: pitch-contour extraction from generated music and comparison against a reference set of melodies the project is allowed to hold (for example public-domain themes or melodies the owner licenses); no copyrighted audio is ingested to build it unless licensed. Record limits honestly.
2. **3.5.2** If approved: `pipeline/validate/melody.py`, check `music_too_close` in `ValidationReport` for any cue intended for publication; cannot-run counts as failed. Tests: `test_transposed_copy_flagged`, `test_unrelated_melody_passes`.

**Estimates:** 4 sessions.

## 3.6 Phase 3 end-to-end

CI with fakes; GPU host live run on one chapter with LoRAs, lip sync and foley; validation report shows `lip_sync` checks; scans clean.

## Cross-cutting steps and owner decisions

- **CI / scans:** trainer image added to hadolint and image scans; no training in CI.
- **Licences/provenance:** `docs/plans/phase-3-model-audit.md`; LoRA provenance (base model, dataset hash, params); foley and music attributions in the bundle.
- **Docker hardening:** trainer has no egress and writes only to staging.
- **Accessibility:** mouth-shape picker and foley editor screens pass axe and keyboard tests.
- **Docs:** README per-profile requirements; results file.
- **Owner decisions:** trainer and base model; L1 lip-sync approach; F1 foley source; melody check acceptance; GPU budget for training.
