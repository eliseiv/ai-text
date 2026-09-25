# Dev shortcuts — thin wrappers around the canonical commands.
.DEFAULT_GOAL := help
.PHONY: help install fmt fmt-check lint type test test-cov test-cov-critical migrate run \
        docker-build up up-obs down logs ci

# Packages where a bug costs money or access — 95% coverage gate EACH.
# The gate is PER-PACKAGE: a single aggregate --cov-fail-under over all of them would let wallet at
# 85% pass on the back of policy at 99% — weakest exactly where it matters most.
CRITICAL_PKGS := policy wallet auth generation billing billing_common billing_adapty \
                 billing_cloudpayments subscription token_purchase

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  %-18s %s\n", $$1, $$2}'

install: ## uv sync (deps + venv)
	uv sync

fmt: ## ruff format (writes changes)
	uv run ruff format .

fmt-check: ## ruff format --check (CI)
	uv run ruff format --check .

lint: ## ruff check
	uv run ruff check .

type: ## mypy src
	uv run mypy src

test: ## pytest
	uv run pytest

test-cov: ## pytest with the global 80% coverage gate
	uv run pytest --cov=src --cov-report=term-missing --cov-fail-under=80

# Reuses the .coverage data written by `test-cov` (so the suite runs ONCE), then gates each
# critical package separately. Run `make test-cov` first — `make ci` does exactly that.
test-cov-critical: ## per-package 95% gate on critical packages (needs .coverage from test-cov)
	@failed=""; \
	for p in $(CRITICAL_PKGS); do \
	  echo "--- coverage gate: app/$$p (>=95%)"; \
	  uv run coverage report --include="src/app/$$p/*" --fail-under=95 --show-missing || failed="$$failed $$p"; \
	done; \
	if [ -n "$$failed" ]; then echo "FAIL: package(s) below 95%:$$failed"; exit 1; fi; \
	echo "all critical packages are >= 95%"

migrate: ## alembic upgrade head
	uv run alembic upgrade head

run: ## run dev server (uvicorn --reload)
	uv run uvicorn app.main:app --reload

ci: fmt-check lint type test-cov test-cov-critical ## run the full local CI gate

docker-build: ## build the image locally
	docker build -t service-backend:local .

up: ## start the full stack (postgres + redis + migrate + api)
	docker compose up --build -d

up-obs: ## start the stack + Prometheus overlay
	docker compose -f docker-compose.yml -f docker-compose.observability.yml up --build -d

down: ## stop the stack and remove volumes
	docker compose -f docker-compose.yml -f docker-compose.observability.yml down -v

logs: ## tail api logs
	docker compose logs -f api
