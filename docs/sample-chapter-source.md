# Sample chapter: source and rights

The end-to-end test chapter. Only this file lives in git; the pages go in the gitignored `data/` folder.

## The work

- **Title:** ブラックジャックによろしく (*Give My Regards to Black Jack*), **volume 1, chapter 1** (if it is under 15 pages, use chapters 1–2).
- **Author:** 佐藤秀峰 (Shuho Sato). Rights holder: 有限会社佐藤漫画製作所 (Sato Manga Works Ltd., operating 電書バト).
- **Format:** Japanese seinen manga, right to left, speech bubbles, screentone, recurring cast. A good match for Kagebox's manga pipeline. The English edition from the same source tests the OCR and translation round trip.
- **Why volume 1:** volumes 2, 7 and 11 contain JASRAC-managed song lyrics that would need separate clearance.
- **Not covered by the terms:** 新ブラックジャックによろしく (the sequel) and the author's other works.

## Licence (verified 2026-11-19 against the rights holder's own page)

A custom free secondary-use licence (二次利用規約), effective 2012-09-15, last revised 2017-08-31, at https://densho810.com/free/. **This is not public domain and not Creative Commons:** copyright is retained, and the rights holder states it will not enforce it. The author confirmed in 2022 that the policy is still in force: https://note.com/shuho_sato/n/n75c0cd1347a4

- Use is allowed commercially or not, with no prior permission and no fee: 「作品を商用・非商用の区別なく、事前の承諾を得ることなく無償で複製し公衆送信し」
- Any adaptation is allowed, explicitly including anime and audio: 「どのような翻案や二次利用（外国語版、パロディ、アニメ化、音声化、小説化、映画化、商品化など）を行うことも可能です。」
- Moral right of integrity not exercised (Japanese terms only; the Japanese text controls): 「著作者人格権（同一性保持権）を行使しません。」

## Conditions Kagebox must honour

1. **Credit on every product, unaltered:** JA 「ブラックジャックによろしく」 佐藤秀峰; EN "Give My Regards to Black Jack" SHUHO SATO. Suggested full credit: *Based on "Give My Regards to Black Jack" by SHUHO SATO (https://densho810.com/free/). Adapted by Kagebox; not produced or endorsed by Shuho Sato or Sato Manga Works.* (Assembly will refuse to export without it; milestone 1.8.)
2. **Report each publication** to info@densho810.com within one month (a URL or image is enough).
3. **Source:** only the free downloads on densho810.com/free/. Never scans of printed books, the separately sold high-resolution data, aggregator sites, or the old parked domains (mangaonweb.com, satoshuho.com).
4. **Distribution:** no Amazon Kindle Store without separate permission; e-book stores priced at 1 yen or more; no illegal sites; no use that presents political or ideological views as the author's.
5. **Voices:** the terms allow audio adaptation. Kagebox's own voice-consent rules still apply.
6. **Terms can change:** the terms pages are snapshotted (date + sha256) as the rights-record evidence.

Rights record in Kagebox: basis `licensed`, licensor 有限会社佐藤漫画製作所, justification "free secondary-use terms (densho810.com/free/, rev. 2017-08-31), adaptation incl. anime permitted", evidence: the terms snapshot.

## Status

| Step | Status |
|---|---|
| Licence verified (research + two independent skeptics) | Done |
| Volume 1 downloaded from densho810.com/free/ (2026-11-19, with the owner's approval) | Done |
| Archives inspected offline, without extracting: one PDF each; no unsafe paths, symlinks, encryption or zip-bomb ratios | Done |
| Chapter 1 page range and count: PDF pages 3–58, 56 pages (第1話 研修医の夜; colour pages 3–6, spread 4–5) | Done (2026-11-19) |
| Rights record created in Kagebox (member submits, admin approves; evidence = PDF wrapping the terms snapshot) | Done (2026-11-19, `make ingest-sample`) |
| Chapter 1 ingested through the API, contact sheets reviewed | Done (2026-11-19); see "Ingest results" |

| File (in `data/fixtures/blackjack-v1/`, gitignored) | Size | sha256 |
|---|---|---|
| `001bj.zip` (Japanese; contains `001bj.pdf`, 354,097,132 bytes) | 343,497,422 | `ec7bbb4ce4f719536a2d58f29eb2c665d19b0769303efe009b4d98cccad699e1` |
| `enbj001.zip` (English; contains `enbj001.pdf`, 419,481,819 bytes) | 403,640,188 | `150e3a2e8d2e71f8f43baf780753463093383bda19b09fbd9b825077da8d13ed` |
| `terms/densho810-free-2026-11-19.html` (terms snapshot, rights evidence) | 32,843 | `2bb1dc9f1a06de8ca66705af93b772f00633cd85c644bd7835025008d43b6da1` |
| `work/001bj.pdf` (extracted from `001bj.zip`; hard-linked as `data/imports/<project_id>/001bj.pdf` by `make ingest-sample`) | 354,097,132 | `a2ad133db82a21cefce1acccb5548de10d2d74118c1519d86ede8566b02ca8b4` |

## Ingest results

First run on 2026-11-19 with `make up-cloud` then `make ingest-sample` (cloud profile, CPU worker, 4 GB limit; no OOM). The import is `001bj.pdf`, pages 3–58, language `ja`, project format `manga_rtl`. The chapter was re-ingested in place after the panel-merge fix (ruling P4, commit `e36a68b`) with `INGEST_CHAPTER_ID=… make ingest-sample`. The report and contact sheets go to `data/reports/` (gitignored, never committed).

**Rights evidence.** The rights endpoint accepts only PDF or images, so `tests/e2e/evidence_pdf.py` wraps the HTML snapshot in a one-page PDF and attaches the snapshot unchanged as an embedded file. The justification also states the snapshot's sha256. `allows_cloud_processing` stays false: ingest runs locally.

| Metric | First run | After the P4 merge (current) |
|---|---|---|
| Pages | 56 (`ready`) | 56 (`ready`) |
| Panels | 255 | 238 (4.3 per page) |
| Bubbles (speech + caption) | 297 | 297 |
| Bubbles below OCR confidence 0.90 (`needs_review`) | 42 (14.1 %) | 42 (14.1 %) |
| Mean OCR confidence | 0.929 (min 0.19) | 0.929 (min 0.19; 226 of 297 at 0.95 or above) |
| Review items | 157: 107 `panel_irregular`, 42 `ocr_low_confidence`, 4 `panel_full_page`, 4 `region_outside_panels` | 157: 95 `panel_irregular`, 42 `ocr_low_confidence`, 12 `panel_merged`, 4 `panel_full_page`, 4 `region_outside_panels` |
| Pages with at least one review item | 51 of 56 (91 %) | 51 of 56 (91 %) |
| Wall time (import accepted → `ready`) | 273 s | 492 s (stack had just restarted; first run is the steady-state figure, about 4.9 s per page) |

**What works.** Dialogue OCR on clean balloons is close to exact. All six balloons on page 7 are read correctly with confidence of 0.97 or more. Right-to-left panel order is correct on page 7 and on the normal pages checked. Full-page art gives one panel: page 4 (the first half of the spread; fill 0.93, so not flagged), page 5 (the second half, see below) and pages 34–37 (flagged `panel_full_page` for confirmation). Balloons and captions are boxed and ordered right to left within each panel.

**Panel merge (ruling P4).** Candidates whose boxes overlap by more than 10 % of the smaller box are merged into one panel, and every merged panel gets a `panel_merged` review item whatever its fill. A merge of real panels can fill its union box well, so the fill test alone would let it through silently. Effect on this chapter:

- **Page 5** (one painting on black paper) went from 4 fragments to **1 panel** (`panel_merged`).
- **Page 6** (colour, black paper, 4 panels) went from 5 to **4 panels**: the spurious nested box is gone, and the order is still correct.
- **Page 20:** the nested box is gone. The diagonal-gutter merge (4 panels in one box) now measures fill 0.88, so `panel_irregular` no longer fires, but `panel_merged` flags it.
- **Over-merges, all flagged `panel_merged`:**
  - page 18: about 6 panels with slanted gutters become 1;
  - page 41: the top panels and a clock strip become one panel;
  - page 49: the bottom 4 panels become 1.
  Smaller merges on pages 10, 17, 40, 43 and 47 are flagged the same way.
- An L-shaped panel's box always contains a panel in its notch, so P4 merges that pair too (flagged).

**Known gaps (sent to review, not fixed here):**

- **Slanted and diagonal gutters** still merge panels (pages 18, 20, 41, 49). The classical detector does not separate them; every case is flagged `panel_merged`.
- **Borderless panels** are missed: on page 23 the explanatory graphic has no frame, so its 4 text regions are `region_outside_panels`.
- **`panel_irregular` is noisy.** 95 of 234 component panels are flagged, many of them correct. Panels that bleed off the page edge or have white interiors measure a low fill even when they are right: on page 7, three correct panels measure 0.26, 0.66 and 0.82.
- **SFX are not a kind.** The detector has no SFX label, so hand-lettered SFX are missed or come out as low-confidence `speech` (page 20). A wall clock was also read as a balloon (low confidence, flagged).
- **Stylised text reads poorly.** Bold outlined narration over art (page 34), signage (page 6) and a sound effect drawn as a balloon (page 20) come out wrong. All of these fall below 0.90 and are flagged.
- **Speaker linking** waits for milestone 1.3. `Bubble.speaker` stays null.

**`panel_irregular_fill` calibration (ruling T1): kept at 0.85.** Counts of component panels below each threshold (first run → after P4):

| Threshold | 0.60 | 0.65 | 0.70 | 0.75 | 0.80 | 0.85 |
|---|---|---|---|---|---|---|
| First run | 49 | 63 | 71 | 84 | 94 | 107 |
| After P4 | 42 | 54 | 61 | 72 | 82 | 95 |

No threshold separates correct panels from real errors. Correct bleed panels on page 7 measure 0.26, 0.66 and 0.82, while real errors measured 0.51–0.80 before the merge. Lowering the threshold would hide real errors, so the evidence does not support a change.

## Studied, not used

JoJo's Bizarre Adventure: Steel Ball Run and Attack on Titan are quality references only; see `docs/design/adaptation-standard.md`. Planet Comics #41 (US public domain) was the earlier candidate and is kept as a fallback for left-to-right comics.
