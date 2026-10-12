SHELL := /bin/bash
PYTHON ?= $(shell command -v python3.12 || command -v python3)
export DBT_SEND_ANONYMOUS_USAGE_STATS := false
export PATH := /opt/homebrew/opt/node@24/bin:$(PATH)

.PHONY: dependencies setup bootstrap demo down demo-reset test check contracts e2e analytics analytics-foundation compose-demo fixtures
dependencies:
	$(PYTHON) -m venv .venv
	.venv/bin/pip install uv==0.12.24
	.venv/bin/uv sync --frozen
	npm ci

setup: bootstrap
	.venv/bin/python -m scripts.operator_db upgrade
	.venv/bin/python -m scripts.operator_db seed
	.venv/bin/python scripts/test_db.py
	.venv/bin/python -m scripts.provision_db --test-db
	.venv/bin/python scripts/generate_workforce_fixtures.py

bootstrap: dependencies
	.venv/bin/python scripts/local.py bootstrap

demo: setup
	npm run build
	.venv/bin/python scripts/local.py up

down:
	.venv/bin/python scripts/local.py down

demo-reset: down
	.venv/bin/python scripts/local.py reset
	.venv/bin/python scripts/local.py up

contracts:
	.venv/bin/python scripts/export_contracts.py
	npm run contracts

check:
	.venv/bin/ruff check apps/api apps/worker scripts
	.venv/bin/ruff format --check apps/api apps/worker scripts
	.venv/bin/mypy
	npm run lint
	npm run typecheck

test: check
	.venv/bin/python scripts/test_db.py
	.venv/bin/pytest --durations=20
	node --env-file=.env --test apps/identity/*.test.mjs
	npm test

e2e:
	npm run e2e

fixtures:
	.venv/bin/python scripts/generate_workforce_fixtures.py

analytics-foundation:
	.venv/bin/python scripts/export_foundation_analytics.py
	.venv/bin/dbt build --project-dir analytics --profiles-dir analytics --select tag:foundation

analytics: analytics-foundation
	.venv/bin/python scripts/verify_business_analytics.py

compose-demo:
	$(PYTHON) scripts/compose_env.py
	docker compose --env-file .local/compose.env -f infra/compose.yaml up --build -d --wait
