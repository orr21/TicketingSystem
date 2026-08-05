VENV := .venv
PY := $(VENV)/bin/python
PIP := $(VENV)/bin/pip
STREAMLIT := $(VENV)/bin/streamlit
ENV_FILE := .env

.PHONY: help install env db-up db-down db-reset run test clean

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "%-10s %s\n", $$1, $$2}'

install: ## Create venv and install dependencies
	python3 -m venv $(VENV)
	$(PIP) install -U pip
	$(PIP) install -r requirements.txt

env: ## Create .env from .env.example if missing
	test -f $(ENV_FILE) || cp .env.example $(ENV_FILE)

db-up: ## Start the local Postgres container
	docker compose up -d db

db-down: ## Stop the local Postgres container
	docker compose down

db-reset: ## Drop all tables so the app re-provisions + re-seeds on next run
	docker compose exec db psql -U postgres -d ticketing -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"

run: env ## Run the app locally (no Databricks/Lakebase needed)
	$(STREAMLIT) run app.py

test: env ## Smoke-test the app (renders the UI against the local DB)
	$(PY) tests/smoke_test.py

clean: ## Remove venv and container data
	docker compose down -v
	rm -rf $(VENV)
