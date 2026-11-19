# Sample chapter: source and public-domain evidence

The end-to-end test chapter. Only this file and its hash live in git; the scan lives in the gitignored `data/` folder.

## The work

- **Story:** "The Lost World", Planet Comics #41 (vol. 1), Fiction House Magazines (indicia: Love Romances Pub. Co., Inc.). Cover date March 1946; on sale **1945-12-28** (GCD, from the CCE Periodicals 1946–47 volume).
- **Credits (GCD):** script "Thornecliffe Herrick" (house name); pencils and inks Lily Renée.
- **Characters:** Hunt Bowman, Lyssa, the Voltamen, Dr. Schultz.
- **Length:** about 8.6 story pages, roughly scan images 3–11 of the 52-page cover-to-cover scan. Confirm the exact indices when ingesting.
- **Format:** comic, left to right. Right-to-left and vertical manga reading orders are covered by synthetic fixtures.
- **Backup** (same file, same evidence): "Gale Allen", Planet Comics #41, about scan images 12–19.

## Public-domain evidence (US only)

1. **It was copyrighted under the 1909 Act.** Copyright Office registrations exist for Planet Comics by Love Romances Publishing Co. (e.g. B 451878, 1940-04-25).
2. **The renewal was due in the 28th year after publication: about 1972-12-28 to 1973-12-28.** The Online Books Page's Copyright Renewals index lists Planet Comics with "no issue renewals found in CCE or CPRS" and "no contribution renewals found in CCE or CPRS": https://onlinebooks.library.upenn.edu/webbin/cinfo/planetcomics
3. **No renewal in the Copyright Office's online records** (searched for "Planet Comics", "Love Romances Publishing", "Fiction House"). Online renewal records mostly start in 1978, so this alone is weak evidence.
4. Hosted as public domain by the Digital Comic Museum (dlid 17239) and Comic Book Plus (dlid 69256), with generic site statements only.

**Limits:** this is public domain in the **US only** (non-renewal). In life+70 countries the art may still be protected (Lily Renée died in 2022). Use it as an internal test fixture and record "US-PD only" in its rights record. Use only the 1946 original scan, never a modern restored or recoloured reprint. This is engineering due diligence, not legal advice.

## Status

| Step | Status |
|---|---|
| Direct check of the CCE 1972–73 renewal volumes (scanned at archive.org and Google Books; periodicals and contributions sections) | **Pending, owner** |
| Download | **Pending, owner.** Comic Book Plus refuses automated downloads, and we don't bypass bot protection. |
| sha256 of the downloaded zip | Pending |
| Rights record in Kagebox (basis: public_domain, justification as above, evidence: this file as PDF) | After download |

## How to add the file

1. In your browser, open https://comicbookplus.com/?dlid=69256 and download the zip (about 24 MB, 52 pages).
2. Move it into the project at `data/fixtures/planet-comics-041/` (gitignored). Don't open or extract it yourself.
3. Tell Claude. The zip is then checked with Kagebox's safe archive reader (no zip-slip, no bombs, images re-encoded), and its sha256 is recorded here.
