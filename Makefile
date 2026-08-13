# Pace Analysis — developer entry points.
# Every Python command runs through the single project virtualenv.

# The root is derived from this makefile, never hardcoded: a copy of the
# repository must operate on itself, not on the tree it was copied from.
ROOT    := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
VENV    ?= $(ROOT)/.venv
PY      := $(VENV)/bin/python
PIP     := $(PY) -m pip
UVICORN := $(VENV)/bin/uvicorn

HOST ?= 127.0.0.1
PORT ?= 8000
DB   ?= data/pace.db

# Reference emails to import; the shell expands the glob (file names contain
# spaces and zero-width spaces, so never type them by hand).
EML ?= *.eml

.DEFAULT_GOAL := help
.PHONY: help install test serve web import clean

help: ## Show this help
	@echo "Pace Analysis — available targets:"
	@echo "  install   install the package with dev extras into $(VENV) (+ npm deps for web/)"
	@echo "  test      run the test suite"
	@echo "  serve     run the FastAPI backend on $(HOST):$(PORT) with autoreload"
	@echo "  web       run the Vite dev server for the frontend"
	@echo "  import    import the reference email(s) matching '$(EML)' into $(DB)"
	@echo "  clean     remove build/test caches (never touches data/)"

install: ## Install Python (and frontend) dependencies
	$(PIP) install -e ".[dev]"
	@if [ -f web/package.json ]; then npm --prefix web install; \
	else echo "web/package.json not found — skipping npm install"; fi

test: ## Run pytest
	$(PY) -m pytest -q

serve: ## Run the API with autoreload
	$(UVICORN) karting.api.app:app --reload --host $(HOST) --port $(PORT)

web: ## Run the frontend dev server
	npm --prefix web run dev

import: ## Import result emails into the SQLite database
	@set -e; \
	found=0; \
	for eml in $(EML); do \
		[ -e "$$eml" ] || continue; \
		found=1; \
		echo "==> importing $$eml"; \
		$(PY) -m karting.cli import "$$eml"; \
	done; \
	if [ $$found -eq 0 ]; then \
		echo "no files matched '$(EML)' — pass EML=path/to/mail.eml"; \
		exit 1; \
	fi

clean: ## Remove caches and build artefacts
	find $(ROOT) -path $(VENV) -prune -o -name '__pycache__' -type d -print0 \
		| xargs -0 rm -rf
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info web/dist
