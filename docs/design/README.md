# Kagebox design reference

Live, clickable version of every screen (open in a browser, press Play to navigate):
https://claude.ai/artifact/6fPYG972D3v9LDvK48RwwG

## What is in this folder

- `screens/*.dc.html` — the source markup of each screen. These files need the design canvas runtime to render, so do not serve them directly. Treat them as the visual spec: layout, spacing, copy, colors and interaction states are all in the markup and in each file's `renderVals()` sample data.
- `logo/` — production SVGs of the mark, app icon and lockups. The lockups use live text (Space Grotesk, Noto Sans JP); convert the text to outlines before using them in print or on systems without those fonts.
- `tokens.json` — color, font, radius and size tokens. Build the Tailwind theme from this file; never hard-code hex values in components.

## Screens

| File | Screen | Purpose |
|---|---|---|
| 00-brand-identity | Brand | Mark, lockups, app icon, palette, type |
| 01-projects | Projects | Series list, format (manga RTL / manhwa vertical / comic LTR), pipeline progress, rights lock, recent jobs |
| 02-panel-reader | Panel reader | Detected panels in reading order; inspector shows every extracted field with its source panel ID, OCR confidence (flag < 0.90), and fields left empty for lack of evidence |
| 03-series-bible | Series bible | Characters, reference crops (approved / rejected / pending), LoRA status, voice profile and consent, identity threshold |
| 04-director-studio | Director studio | Shot list, source keyframe next to generated take, takes, validation checks, accept / regenerate / send to review, multi-track timeline |
| 05-voice-casting | Voice casting | Each character with current voice and a sample of one of their real lines; voice library previews the selected character's line; swap with undo; per-language casting; consent-locked voices |
| 06-art-direction | Art direction | Source vs CGI-to-avoid vs hand-drawn target; frame timing (ones / twos / threes / mixed), shading, camera, line-art lock, smears, interpolation scope, style guard |

## Design rules

- Dark studio theme so the artwork is the brightest thing on screen.
- Status colors are blue (pass) and amber (needs review) — never red/green alone, and always with a text label.
- WCAG 2.2 AA contrast, real buttons / links / labels, 44 px minimum touch targets, full keyboard navigation.
- Side navigation collapses above the content on narrow screens; wide tables scroll inside their own container.
- Use straight sample data placeholders like `[ACTOR NAME]` until real data exists; never invent metrics.

## Adaptation standard

The quality bar for every episode (shot fidelity, limited-animation timing, drawn look, pacing, score) is in [`adaptation-standard.md`](adaptation-standard.md). It was written from studying acclaimed adaptations; none of their material enters Kagebox.
