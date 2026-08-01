PYTHON ?= python3
VENV := .venv

.PHONY: setup test demo migrate server worker

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -e ".[dev]"

test:
	$(VENV)/bin/python -m ruff check .
	$(VENV)/bin/python -m pytest

demo:
	$(VENV)/bin/python -m evidence_delta.demo

migrate:
	$(VENV)/bin/alembic upgrade head

server:
	$(VENV)/bin/uvicorn evidence_delta.api:app --reload

worker:
	$(VENV)/bin/evidence-delta-worker
