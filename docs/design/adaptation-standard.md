# Adaptation standard

The quality bar for every Kagebox episode. It is written from studying acclaimed manga-to-anime adaptations, for example David Production's JoJo's Bizarre Adventure series and MAPPA's Attack on Titan, including Hiroyuki Sawano's scores for the latter.

**Learn, don't copy.** These works are studied by people, from legitimately obtained copies, and turned into the written rules below. Their pages, frames, audio, character designs and melodies never enter Kagebox: no ingesting, no fixtures, no reference audio for music generation, no "in the style of <artist>" prompts, and no similarity scoring against them. Every project still needs an approved rights record for its own source material.

Each rule names where it is enforced: the `StyleProfile`, the director's shot rules, a validation check, or the review checklist.

## 1. Fidelity to the page

| Rule | Enforced by |
|---|---|
| The source panel is the keyframe. The first or held frame of a shot reproduces the panel's composition, framing and line art. | Director (keyframes = `source_panel_ids`), style guard |
| Never invent characters, outfits, props, dialogue or settings that the panels don't show. Fields without visual evidence stay empty. | Schemas (`Cited`), director validator |
| Keep the artist's signature graphics as part of the image: sound effects lettered into the art, speed lines, impact frames, screentone. | Layer separation keeps SFX lettering on its own layer; review checklist |
| Character designs match the series bible on every frame (identity threshold per character). | Identity check |

## 2. Motion: limited animation done on purpose

| Rule | `StyleProfile` field / enforced by |
|---|---|
| Default timing is mixed: dialogue and calm beats on threes, action on twos, ones only for a few key motions. | `frame_timing = mixed` |
| Hold dramatic frames. A reveal or a "pose" panel is held still for 1–3 s while the camera or effects move. | Director duration rules (below) |
| Move the camera across still art (multiplane pans, push-ins, parallax) before redrawing a character. | `camera = multiplane`, `interpolation_scope = camera_only` |
| Use smears, speed lines and impact frames for fast motion. Never motion blur. | `smear_frames = true`, `negative_prompts` includes motion blur |
| Mouth flaps use a few drawn mouth shapes timed to the voice line, not continuous lip morphing. | Phase 3 lip sync; review checklist until then |

## 3. Look: drawn, not rendered

| Rule | `StyleProfile` field |
|---|---|
| Lock line art to the source drawing; frame-by-frame line consistency is checked. | `lock_line_art = true` |
| Flat cel colour from the series palette, with hard-edged shadows (cel2) or screentone for black-and-white sources. | `shading`, `palette` |
| Never use a 3D or CGI look: no soft gradients, plastic skin, volumetric light, depth of field or glow. | `negative_prompts`, style guard (`style_threshold` 0.85) |

## 4. Pacing (director rules)

Starting durations, which the director may adjust within ±40% and an editor can override:

| Panel kind | Duration | Typical camera |
|---|---|---|
| Establishing / wide | 3–5 s | slow pan or push-in |
| Dialogue | voice line length + 0.5 s lead-in and tail | static or slight push |
| Reaction / close-up | 1–2 s | static, optional impact zoom |
| Action beat | 0.5–1.5 s per panel | fast pan, shake, smear frames |
| Reveal / splash page | 2–4 s hold | slow push-in, then cut on a music hit |

- Transitions are mostly cuts. Dissolves only for time skips; fade to black only at scene ends.
- Reading order sets shot order. A multi-panel action sequence may become one shot if the panels share a background (multiplane pan across them).

## 5. Sound and score

| Rule | Enforced by |
|---|---|
| The score is written per scene from a mood cue sheet: emotion, intensity curve, and the hit points where the music must land (reveals, impacts, scene ends). | Cue sheet schema (milestone 1.6), score draft |
| Target palette, described never imitated: hybrid orchestral with strings and brass, choir for climaxes, driving percussion, electronic textures for tension, quiet piano or solo instruments under dialogue. | Cue sheet descriptors; `negative_prompts` for music bans artist and title names |
| Music builds into key beats and drops or cuts out just before a reveal, so silence carries the moment. | Cue sheet hit points |
| Dialogue always stays clear: the score is ducked under voice lines. | Assemble mix rules |
| Foley follows the lettered sound effects on the page; voice timing follows bubble order. | Audio stage, traceability check |
| Generated music must not reproduce existing melodies. A melody-similarity check against known themes comes later. | Phase 3 backlog |

## 6. Review checklist (human, per scene)

1. Every shot traces to its source panels, and nothing was invented.
2. Characters are on-model in every frame.
3. It reads as drawn: lines held, flat colour, no CGI look.
4. Timing has holds on reveals; action uses smears, not blur.
5. The music lands on the hit points, and dialogue is never masked.
6. Voices are the cast voices with signed consent; the watermark is present.
