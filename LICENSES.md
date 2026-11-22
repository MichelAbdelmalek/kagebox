# Third-party components and licenses

Every model, service image and direct dependency Kagebox uses is listed here with its license
and source. Model entries also live in `configs/models.yaml` with pinned revisions, and each
generated artifact carries a provenance manifest naming the exact model and revision.

Status: **Phase 1, milestone 1.0.** Models below were verified and approved on 2026-11-19
(docs/plans/phase-1-model-audit.md). Magi v2 was rejected (non-commercial terms, unlicensed code).

## Models

| Model | Stage | Version / revision | License | Source |
|---|---|---|---|---|
| SigLIP so400m (google/siglip-so400m-patch14-384) | bible: character embeddings | `9fdffc58afc957d1a03a25b10dba0329ab15c2a3` | Apache-2.0 | https://huggingface.co/google/siglip-so400m-patch14-384 |
| SAM 2.1 hiera-small (facebook/sam2.1-hiera-small) | render: layer masks | `ee5bba1d82bb8749febdf90f45e84b687142ba03` | Apache-2.0 | https://huggingface.co/facebook/sam2.1-hiera-small |
| Depth Anything V2 **Small** (depth-anything/Depth-Anything-V2-Small-hf) | render: depth order | `5426e4f0f36572d16453bbda7a8389317b1bef99` | Apache-2.0 (Small only; larger sizes are CC-BY-NC and excluded) | https://huggingface.co/depth-anything/Depth-Anything-V2-Small-hf |
| PP-OCRv5 mobile det (PaddlePaddle/PP-OCRv5_mobile_det_safetensors) | ingest: text detection | `c5041d225cf951ff06900ab81a3c7d543c45e2ad` | Apache-2.0 | https://huggingface.co/PaddlePaddle/PP-OCRv5_mobile_det_safetensors |
| PP-OCRv5 mobile rec, English | ingest: OCR (EN) | `e4abce2bbd335f6c3eda0507b7f8e8abd5af3419` | Apache-2.0 | https://huggingface.co/PaddlePaddle/en_PP-OCRv5_mobile_rec_safetensors |
| PP-OCRv5 mobile rec, Korean | ingest: OCR (KO) | `6ef525a357645ce46495c7ed1fef622c4e009e7a` | Apache-2.0 | https://huggingface.co/PaddlePaddle/korean_PP-OCRv5_mobile_rec_safetensors |
| manga-bubble-detector (NorwayFish) | ingest: speech bubbles | `bcf342f296beb56005e044da15990502629015de` | Apache-2.0 (model-card tag only; training pages undisclosed, open legal item) | https://huggingface.co/NorwayFish/manga-bubble-detector |
| Whisper large-v3-turbo (openai/whisper-large-v3-turbo) | validate: speech round-trip | `41f01f3fe87f28c78e2fbf8b568835947dd65ed9` | MIT | https://huggingface.co/openai/whisper-large-v3-turbo |
| manga-ocr (kha-white/manga-ocr-base), **converted offline to safetensors** | ingest: Japanese OCR | `aa6573bd10b0d446cbf622e29c3e084914df9741` | **CC-BY-SA-4.0** (card says Apache-2.0; the decoder inherits CC-BY-SA-4.0 from cl-tohoku/bert-base-japanese-char-v2). Attribute; share-alike applies to the model weights | https://huggingface.co/kha-white/manga-ocr-base |
| Qwen3-VL-2B-Instruct (Qwen/Qwen3-VL-2B-Instruct) | understand (approved 2026-11-22): local vision model | `89644892e4d85e24eaac8bacfd4f463576704203` | Apache-2.0 | https://huggingface.co/Qwen/Qwen3-VL-2B-Instruct/tree/89644892e4d85e24eaac8bacfd4f463576704203 |
| Qwen3.5-2B (Qwen/Qwen3.5-2B) | understand (candidate): local vision model | `15852e8c16360a2fea060d615a32b45270f8a8fc` | Apache-2.0 | https://huggingface.co/Qwen/Qwen3.5-2B/tree/15852e8c16360a2fea060d615a32b45270f8a8fc |
| Qwen3.5-0.8B (Qwen/Qwen3.5-0.8B) | understand (candidate): local vision model | `2fc06364715b967f1860aea9cf38778875588b17` | Apache-2.0 | https://huggingface.co/Qwen/Qwen3.5-0.8B/tree/2fc06364715b967f1860aea9cf38778875588b17 |
| gpt-oss-120b via Together AI (openai/gpt-oss-120b) | director: shot list | upstream `b5c939de8f754692c1647ca79fbf85e8c1e70f8a` (served build not verifiable) | Apache-2.0 | https://huggingface.co/openai/gpt-oss-120b |

Exact files, sizes and sha256 values are in `configs/models.yaml`. Converted offline (pickle source quarantined, output pinned, checked against Hugging Face's own safetensors copy): manga-ocr. Coming with milestones 1.5–1.6 through the same job: CosyVoice2-0.5B (Apache-2.0), AudioSeal (MIT), Big-LaMa (Apache-2.0). Scene understanding: Qwen3-VL-2B-Instruct (Apache-2.0), approved by the owner on 2026-11-22 after the milestone 1.2 bake-off (`docs/plans/phase-1-model-audit.md`); Qwen3.5-2B and Qwen3.5-0.8B (both Apache-2.0) stay on disk as candidates only. Reference works studied for the adaptation standard are listed in `docs/design/adaptation-standard.md`; none of their material is used.

## Service images (pinned by digest in `deploy/compose.yaml`)

| Component | Version | License | Source |
|---|---|---|---|
| Caddy | 2.10 (alpine) | Apache-2.0 | https://github.com/caddyserver/caddy |
| PostgreSQL | 17.6 (alpine) | PostgreSQL License | https://www.postgresql.org |
| Qdrant | 1.15.5 (unprivileged) | Apache-2.0 | https://github.com/qdrant/qdrant |
| SeaweedFS (S3 object store) | 3.97 | Apache-2.0 | https://github.com/seaweedfs/seaweedfs |
| Prefect (server and workers, from our image) | 3.8.x | Apache-2.0 | https://github.com/PrefectHQ/prefect |
| Python base image | 3.11.18-slim-bookworm (+ Debian security updates at build) | PSF-2.0 (+ Debian packages) | https://hub.docker.com/_/python |
| Node base image | 22.21.1-bookworm-slim | MIT (+ Debian packages) | https://hub.docker.com/_/node |
| Distroless Node.js runtime | nodejs22-debian13 | Apache-2.0 | https://github.com/GoogleContainerTools/distroless |
| BusyBox (volume ownership job) | 1.37.0 | GPL-2.0 (unmodified, run as a tool) | https://busybox.net |

## Python dependencies (direct; exact versions and hashes in `uv.lock`)

| Package | License |
|---|---|
| pydantic, pydantic-settings | MIT |
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| sqlalchemy, alembic | MIT |
| psycopg | LGPL-3.0 (used unmodified as a library) |
| argon2-cffi | MIT |
| python-multipart | Apache-2.0 |
| filetype | MIT |
| pillow | MIT-CMU (HPND) |
| httpx | BSD-3-Clause |
| python-ulid | MIT |
| pyyaml | MIT |
| prefect | Apache-2.0 |
| boto3 | Apache-2.0 |
| dev: pytest, ruff, mypy, bandit, pip-audit, respx | MIT / Apache-2.0 |

## Web dependencies (direct; exact versions in `apps/web/pnpm-lock.yaml`)

| Package | License |
|---|---|
| next, react, react-dom | MIT |
| tailwindcss, @tailwindcss/postcss | MIT |
| @fontsource-variable/inter, space-grotesk, jetbrains-mono | OFL-1.1 (fonts), MIT (packaging) |
| dev: typescript | Apache-2.0 |
| dev: eslint, eslint-config-next, vitest, @testing-library/react, jsdom | MIT |

## Hosted services

| Service | Used for | Terms |
|---|---|---|
| Together AI | Phase 1: scene understanding (VLM) and director (LLM), cloud profile | Pages are sent only when the project's rights record allows hosted processing. Check your account's data-retention settings. |
