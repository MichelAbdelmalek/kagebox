# syntax=docker/dockerfile:1.10
# Python services: api, worker-cpu, prefect-server, jobs. Select with --target and EXTRA.
ARG UV_IMAGE=ghcr.io/astral-sh/uv:python3.11-bookworm-slim@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36
ARG PY_IMAGE=python:3.11-slim-bookworm@sha256:a83ba18aef0f865b1a4a611c2cf460fe446f2795673bbb9fe43be570439e3b39

FROM ${UV_IMAGE} AS build
ARG EXTRA
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project --extra "${EXTRA}"
COPY pipeline ./pipeline
COPY orchestration ./orchestration
COPY apps/api/kagebox_api ./apps/api/kagebox_api
# --reinstall-package: the project version rarely changes, so uv would reuse a stale wheel.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --reinstall-package kagebox --extra "${EXTRA}"

FROM ${PY_IMAGE} AS runtime
# Apply Debian security updates on top of the pinned base, and drop the system pip,
# setuptools and wheel: the app runs from its own venv and never installs at runtime.
# hadolint ignore=DL3005
RUN apt-get update \
 && apt-get -y upgrade --no-install-recommends \
 && rm -rf /var/lib/apt/lists/* \
 && python -m pip uninstall -y -q pip setuptools wheel \
 && groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=build --chown=root:root /app/.venv /app/.venv
COPY --chown=root:root configs /app/configs
ENV PATH=/app/.venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    KAGEBOX_CONFIG_DIR=/app/configs \
    HF_HUB_OFFLINE=1 \
    PREFECT_HOME=/tmp/prefect
USER 10001:10001

FROM runtime AS api
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health',timeout=2).status==200 else 1)"]
CMD ["uvicorn", "kagebox_api.main:app_factory", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header"]

FROM runtime AS worker
# FFmpeg for render and assembly (Debian build, GPL: fine to run as a service; revisit before
# distributing images). Unpinned apt versions: we take Debian's current security releases.
USER root
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*
USER 10001:10001
ENV KAGEBOX_MODELS_DIR=/models \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    OMP_NUM_THREADS=4
CMD ["python", "-m", "orchestration.serve"]
