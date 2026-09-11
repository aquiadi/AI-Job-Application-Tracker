SHELL := /bin/bash
.DEFAULT_GOAL := help
.ONESHELL:

# Targets appear here when the thing they run exists. The full intended set is in
# docs/SPEC.md; anything missing is listed in docs/ROADMAP.md with the milestone
# that adds it. A target that fails because its script has not been written yet is
# worse than no target.

UV ?= uv
COMPOSE ?= docker compose
WEB_DIR := apps/web

# Every Python entry point goes through `uv run` so there is one source of truth for
# the interpreter and the lockfile, and no activated virtualenv to forget.
#
# PYTHONPATH is set explicitly rather than relying on uv's editable installs. Those
# work through .pth files in site-packages, and CPython skips any .pth carrying
# macOS's UF_HIDDEN flag, which iCloud Drive sets on everything inside a synced
# ~/Documents. The symptom is an import error that appears and disappears with the
# sync; naming the source roots removes the dependency. Containers install the
# packages normally and are unaffected.
export PYTHONPATH := packages/core/src:services/api/src:services/worker/src:evals/src

PY := $(UV) run --

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | sort \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[1m%-16s\033[0m %s\n", $$1, $$2}'

# --------------------------------------------------------------------------
# Setup
# --------------------------------------------------------------------------

.PHONY: install
install: ## Install Python and web dependencies
	$(UV) sync --all-packages
	cd $(WEB_DIR) && npm ci

.PHONY: up
up: ## Start Postgres, the Pub/Sub emulator, and the Auth emulator
	$(COMPOSE) up -d --wait

.PHONY: down
down: ## Stop the local stack (keeps the database volume)
	$(COMPOSE) down

.PHONY: clean
clean: ## Stop the local stack and delete its data
	$(COMPOSE) down -v

# --------------------------------------------------------------------------
# Development
# --------------------------------------------------------------------------

.PHONY: dev
dev: up ## Run the local stack: api on 8080, worker on 8081, web on 3000
	@trap 'kill 0' EXIT INT TERM
	$(PY) uvicorn jobtrack_api.main:app --reload --port 8080 &
	$(PY) uvicorn jobtrack_worker.main:app --reload --port 8081 &
	cd $(WEB_DIR) && npm run dev &
	wait

# --------------------------------------------------------------------------
# Quality gates. `check` is what must be green before every commit.
# --------------------------------------------------------------------------

.PHONY: check
check: lint typecheck test web-check ## Everything that gates a commit

.PHONY: lint
lint: ## Lint and check formatting (Python)
	$(UV) run ruff check .
	$(UV) run ruff format --check .

.PHONY: fmt
fmt: ## Auto-format and auto-fix (Python and web)
	$(UV) run ruff check --fix .
	$(UV) run ruff format .
	cd $(WEB_DIR) && npm run fmt

.PHONY: typecheck
typecheck: ## mypy --strict over service and package code
	$(UV) run mypy packages services evals

.PHONY: test
test: ## Run unit tests (no network, no credentials)
	$(UV) run pytest -m "not integration and not live"

.PHONY: test-integration
test-integration: up migrate ## Tests needing the local stack, including cross-tenant RLS
	$(UV) run pytest -m integration

.PHONY: web-check
web-check: ## Typecheck, lint, format-check and contrast-check the web app
	cd $(WEB_DIR) && npm run check

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

.PHONY: migrate
migrate: ## Apply migrations to the local database and the test database
	$(UV) run alembic -c packages/core/alembic.ini upgrade head
	DB_NAME=jobtrack_test $(UV) run alembic -c packages/core/alembic.ini upgrade head

.PHONY: migration
migration: ## Create a migration: make migration m="add nudges table"
	@test -n "$(m)" || { echo 'usage: make migration m="describe the change"'; exit 1; }
	$(UV) run alembic -c packages/core/alembic.ini revision --autogenerate -m "$(m)"

# --------------------------------------------------------------------------
# Google Cloud
# --------------------------------------------------------------------------

.PHONY: sandbox-check
sandbox-check: ## Verify the sandbox project can run this system
	./scripts/sandbox_check.sh
