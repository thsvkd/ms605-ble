.DEFAULT_GOAL := help

ARGS ?=

.PHONY: help setup run cli gui test test-python test-web lint typecheck web-setup web-dev web-build web-typegen

help:
	@printf '%s\n' \
	  'make setup          Install Python dependencies (CLI + GUI server)' \
	  'make cli            Run the interactive CLI (alias: make run)' \
	  'make gui            Restart the web GUI server (LAN + available tailnet URLs)' \
	  '                    Pass options with ARGS="--help" or ARGS="--sim 7"' \
	  'make web-setup      Install frontend dev dependencies (Node 22 + npm)' \
	  'make web-dev        Run the frontend development server (backend: make gui)' \
	  'make test           Run Python + frontend tests, lint and typecheck' \
	  'make test-python    Run offline Python tests (CLI + GUI backend)' \
	  'make test-web       Run frontend tests' \
	  'make lint           Run Python lint' \
	  'make typecheck      Check frontend TypeScript' \
	  'make web-build      Build the bundled GUI frontend' \
	  'make web-typegen    Regenerate frontend types from backend OpenAPI'

setup:
	./scripts/setup.sh

run: cli

cli:
	./scripts/run.sh $(ARGS)

gui:
	uv run python scripts/gui.py $(ARGS)

test: test-python test-web lint typecheck

test-python:
	uv run pytest -q

test-web:
	npm --prefix web test

lint:
	uv run ruff check .

typecheck:
	npm --prefix web run typecheck

web-setup:
	npm --prefix web ci

web-dev:
	npm --prefix web run dev

web-build:
	npm --prefix web run build

web-typegen:
	npm --prefix web run typegen
