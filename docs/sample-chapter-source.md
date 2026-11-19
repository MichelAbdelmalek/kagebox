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
| Chapter 1 page range and count | Milestone 1.1 (needs the sandboxed PDF renderer) |
| Rights record created in Kagebox | Milestone 1.1, with the ingest upload |

| File (in `data/fixtures/blackjack-v1/`, gitignored) | Size | sha256 |
|---|---|---|
| `001bj.zip` (Japanese; contains `001bj.pdf`, 354,097,132 bytes) | 343,497,422 | `ec7bbb4ce4f719536a2d58f29eb2c665d19b0769303efe009b4d98cccad699e1` |
| `enbj001.zip` (English; contains `enbj001.pdf`, 419,481,819 bytes) | 403,640,188 | `150e3a2e8d2e71f8f43baf780753463093383bda19b09fbd9b825077da8d13ed` |
| `terms/densho810-free-2026-11-19.html` (terms snapshot, rights evidence) | 32,843 | `2bb1dc9f1a06de8ca66705af93b772f00633cd85c644bd7835025008d43b6da1` |

## Studied, not used

JoJo's Bizarre Adventure: Steel Ball Run and Attack on Titan are quality references only; see `docs/design/adaptation-standard.md`. Planet Comics #41 (US public domain) was the earlier candidate and is kept as a fallback for left-to-right comics.
