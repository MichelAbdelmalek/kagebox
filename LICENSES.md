# Third-party components and licenses

Every model, service image and direct dependency Kagebox uses is listed here with its license
and source. Model entries also live in `configs/models.yaml` with pinned revisions, and each
generated artifact carries a provenance manifest naming the exact model and revision.

Status: **Phase 0**. No models are approved yet; the Phase 1 candidates (Magi v2, manga-ocr,
PaddleOCR, Qwen-VL, SigLIP, SAM 2.1, Depth Anything V2 Small, LaMa, CosyVoice2, AudioSeal,
ACE-Step, Whisper) are added here once their licenses are verified and you approve them.

## Models

| Model | Stage | Version / revision | License | Source |
|---|---|---|---|---|
| _none approved yet_ | | | | |

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
