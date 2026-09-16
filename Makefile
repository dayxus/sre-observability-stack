# SRE observability stack — one entrypoint for the local loop and for CI.
#
#   make setup      create .venv with the test/lint tooling
#   make test       run the test suite (no docker needed)
#   make validate   promtool + amtool + loki + promtail + pytest + compose config
#   make up         start the stack (generates TLS material, waits for healthchecks)
#   make smoke      prove the running stack, including alert delivery and log shipping

SHELL := /bin/bash
.DEFAULT_GOAL := help
PYTHON ?= python3
VENV := .venv
PYTEST := $(VENV)/bin/python -m pytest

.PHONY: help setup test lint validate up down smoke logs ps versions audit clean

help: ## Show the available targets
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk -F':.*?## ' '{printf "  \033[36m%-10s\033[0m %s\n", $$1, $$2}'

setup: ## Create .venv and install the test and lint tooling
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -U pip
	$(VENV)/bin/python -m pip install -r requirements-dev.txt

test: ## Run the test suite: dashboards, compose, configs, synthetic target
	@test -x $(VENV)/bin/pytest || $(MAKE) --no-print-directory setup
	$(PYTEST) tests/ -q

lint: ## ruff check and ruff format --check
	@test -x $(VENV)/bin/ruff || $(MAKE) --no-print-directory setup
	$(VENV)/bin/ruff check .
	$(VENV)/bin/ruff format --check .

validate: ## Validate everything that does not need a cluster
	./scripts/validate.sh

up: ## Start the stack and wait for every healthcheck
	./scripts/up.sh

down: ## Stop the stack and remove its volumes
	./scripts/down.sh

smoke: ## Prove the running stack: targets, probes, dashboards, alerts, logs
	./scripts/smoke.sh

logs: ## Follow the stack logs
	bash -c '. ./scripts/lib/env.sh && docker compose logs -f --tail=200'

ps: ## Show the stack containers with their health
	bash -c '. ./scripts/lib/env.sh && docker compose ps'

versions: ## Regenerate docs/versions.md and reports/weekly-audit.md from the upstream APIs
	$(PYTHON) scripts/audit_versions.py

audit: versions ## Alias of versions

clean: ## Remove local state: runtime/, .audit/ and the python caches
	rm -rf runtime .audit .pytest_cache .ruff_cache
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
