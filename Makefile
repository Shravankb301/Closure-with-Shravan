PYTHON ?= python3
VENV := .venv

.PHONY: setup test demo server

setup:
	$(PYTHON) -m venv $(VENV)
	$(VENV)/bin/python -m pip install -e ".[dev,postgres]"

test:
	$(VENV)/bin/python -m ruff check .
	$(VENV)/bin/python -m pytest

demo:
	$(VENV)/bin/python -m evidence_delta.demo

server:
	$(VENV)/bin/uvicorn evidence_delta.api:app --reload
