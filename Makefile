.DEFAULT_GOAL := help
SHELL := /bin/bash
UV ?= uv
COMPOSE_DEV := docker compose -f docker-compose.dev.yml

.PHONY: help install dev dev-down lint typecheck test test-unit eval migrate seed cov check-coverage clean bench-light worker-light

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

install: ## Install project and dev dependencies into .venv (uv)
	$(UV) sync --all-groups --frozen

dev: install ## Start the full dev environment (Postgres, ClickHouse, Redis, MinIO, Unbound, Caddy), apply migrations and seed references
	$(COMPOSE_DEV) up -d --wait
	$(MAKE) migrate
	$(MAKE) seed

dev-down: ## Stop the dev environment and drop its volumes
	$(COMPOSE_DEV) down -v

lint: ## ruff (format check + lint)
	$(UV) run ruff format --check src tests alembic scripts
	$(UV) run ruff check src tests alembic scripts

fmt: ## Auto-format with ruff
	$(UV) run ruff format src tests alembic scripts
	$(UV) run ruff check --fix src tests alembic scripts

typecheck: ## mypy --strict
	$(UV) run mypy

test: ## Full test-suite with coverage (needs Docker for testcontainers)
	$(UV) run pytest --cov --cov-report=term-missing --cov-report=json:coverage.json
	$(UV) run python scripts/check_coverage.py coverage.json

test-unit: ## Unit tests only (no containers)
	$(UV) run pytest tests/unit

migrate: ## Apply Postgres (Alembic) and ClickHouse migrations
	$(UV) run payintel migrate

seed: ## Load reference dictionaries and detection rules into the database
	$(UV) run payintel seed

eval: ## Compute precision/recall/F1 on the gold set (FR-QA-02); fails if PSP precision < threshold
	$(UV) run payintel eval --report eval-report.json

bench-light: ## NFR-P-01 load run of the light scanner against a local fixture server (not in CI)
	$(UV) run python scripts/bench_light.py --domains $${BENCH_DOMAINS:-500} --concurrency $${BENCH_CONCURRENCY:-200}

worker-light: ## Run one light-scan worker loop against the dev environment
	$(UV) run payintel worker-light

clean: ## Remove caches and build artifacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache coverage.json .coverage htmlcov dist build
