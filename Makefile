# Everything runs in containers; nothing is installed on the host.
SHELL := /bin/sh
COMPOSE := docker compose -f deploy/compose.yaml --env-file .env

UV_IMAGE := ghcr.io/astral-sh/uv:python3.11-bookworm-slim@sha256:4f5d923c9dcea037f57bda425dd209f3ec643da2f0b74227f68d09dab0b3bb36
NODE_IMAGE := node:22.21.1-bookworm-slim@sha256:25b3eb23a00590b7499f2a2ce939322727fcce1b15fdd69754fcd09536a3ae2c
HADOLINT_IMAGE := hadolint/hadolint:v2.12.0-debian@sha256:27173fe25e062448490a32de410c08491c626a0bef360aa2ce5d5bdd9384b50d
TRIVY_IMAGE := aquasec/trivy:0.67.2@sha256:e2b22eac59c02003d8749f5b8d9bd073b62e30fefaef5b7c8371204e0a4b0c08
GITLEAKS_IMAGE := zricethezav/gitleaks:v8.28.0@sha256:cdbb7c955abce02001a9f6c9f602fb195b7fadc1e812065883f695d1eeaba854

UV := docker run --rm -v "$(CURDIR)":/app -v kagebox-venv:/app/.venv -v kagebox-uvcache:/root/.cache/uv -w /app -e UV_LINK_MODE=copy $(UV_IMAGE) uv
PNPM := docker run --rm -v "$(CURDIR)/apps/web":/app -v "$(CURDIR)/docs":/docs:ro -v kagebox-web-nm:/app/node_modules -v kagebox-pnpm:/root/.local/share/pnpm -w /app -e COREPACK_ENABLE_DOWNLOAD_PROMPT=0 -e npm_config_store_dir=/root/.local/share/pnpm/store -e CI=true $(NODE_IMAGE) sh -c

.PHONY: help secrets env up-cloud up-gpu down logs ps admin user test test-py test-web lint scan scan-images pin-models fetch-models convert-models check-models worker-image

help:
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

env: ## create .env from .env.example if missing
	@test -f .env || cp .env.example .env

secrets: ## generate missing Docker secrets in deploy/secrets (never overwrites)
	docker run --rm -v "$(CURDIR)/deploy":/deploy -w /deploy $(UV_IMAGE) python -I scripts/gen_secrets.py /deploy/secrets

up-cloud: env secrets ## start the stack, generation on Together AI (default)
	KAGEBOX_PROFILE=cloud $(COMPOSE) up -d --build --wait

up-gpu: env secrets ## start the stack with worker-gpu (needs NVIDIA Container Toolkit)
	KAGEBOX_PROFILE=gpu $(COMPOSE) --profile gpu up -d --build --wait

down: ## stop the stack (volumes are kept)
	$(COMPOSE) --profile gpu --profile fetch down

logs: ## follow logs
	$(COMPOSE) logs -f --tail=100

ps:
	$(COMPOSE) ps

admin: ## create an admin user (prompts for email and password)
	@read -p "Admin email: " email; $(COMPOSE) exec api python -m kagebox_api.cli create-user --admin --email "$$email"

user: ## create a member user (prompts for email and password)
	@read -p "User email: " email; $(COMPOSE) exec api python -m kagebox_api.cli create-user --email "$$email"

test: test-py test-web ## run all unit tests

test-py:
	$(UV) sync -q --locked --all-extras --group dev
	$(UV) run pytest -q

test-web:
	$(PNPM) 'corepack enable && pnpm install --frozen-lockfile --reporter=silent && pnpm -s tokens:check && pnpm -s typecheck && pnpm -s lint && pnpm -s test'

lint: ## ruff, mypy, bandit
	$(UV) run ruff check .
	$(UV) run ruff format --check .
	$(UV) run mypy
	$(UV) run bandit -q -c pyproject.toml -r pipeline orchestration apps/api

scan: lint ## security scans: pip-audit, pnpm audit, hadolint, trivy, gitleaks (fail on high/critical)
	# torch/torchvision come from PyTorch's CPU index (+cpu builds); audit them as the same upstream release.
	$(UV) run sh -c 'uv export -q --locked --all-extras --no-dev --no-emit-project -o /tmp/r.txt && sed -E -i "s/^(torch(vision)?==[0-9.]+)\+cpu/\1/" /tmp/r.txt && grep -E "^torch(vision)?==" /tmp/r.txt && pip-audit --strict --disable-pip -r /tmp/r.txt'
	$(PNPM) 'corepack enable && pnpm audit --prod --audit-level high'
	docker run --rm -i -v "$(CURDIR)/.hadolint.yaml":/.hadolint.yaml:ro $(HADOLINT_IMAGE) hadolint --config /.hadolint.yaml - < deploy/docker/python.Dockerfile
	docker run --rm -i -v "$(CURDIR)/.hadolint.yaml":/.hadolint.yaml:ro $(HADOLINT_IMAGE) hadolint --config /.hadolint.yaml - < deploy/docker/web.Dockerfile
	docker run --rm -v "$(CURDIR)":/src:ro -v kagebox-trivy:/root/.cache $(TRIVY_IMAGE) fs --scanners vuln,misconfig,secret --severity HIGH,CRITICAL --exit-code 1 --skip-dirs /src/apps/web/node_modules --skip-dirs /src/.venv --skip-dirs /src/data /src
	docker run --rm -v "$(CURDIR)":/repo:ro $(GITLEAKS_IMAGE) git --no-banner /repo

scan-images: ## trivy scan of the built images (exported tarballs; the Docker socket is never mounted)
	mkdir -p .scan
	for i in api worker web; do \
	  docker save -o .scan/$$i.tar kagebox/$$i:dev && \
	  docker run --rm -v "$(CURDIR)/.scan":/scan:ro -v kagebox-trivy:/root/.cache $(TRIVY_IMAGE) image --input /scan/$$i.tar --severity HIGH,CRITICAL --ignore-unfixed --exit-code 1 || exit 1; done
	rm -rf .scan

pin-models: ## regenerate pinned files/hashes in configs/models.yaml from models.sources.yaml
	$(UV) run python deploy/scripts/pin_models.py configs

worker-image:
	$(COMPOSE) build prefect-server

fetch-models: worker-image ## download pinned model files (pickle sources go to quarantine only)
	$(COMPOSE) --profile fetch run --rm models-fetch

check-models: worker-image ## offline: verify hashes, load every approved model, run one inference
	$(COMPOSE) --profile fetch run --rm models-check

convert-models: worker-image ## offline: pickle -> safetensors in a sandbox, then promote pinned outputs
	$(COMPOSE) --profile fetch run --rm models-convert
	$(COMPOSE) --profile fetch run --rm models-promote
