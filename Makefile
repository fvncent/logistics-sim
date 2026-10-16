# Docker Desktop on macOS does not always put the CLI on PATH; fall back to the app bundle.
DOCKER ?= $(shell command -v docker 2>/dev/null || echo /Applications/Docker.app/Contents/Resources/bin/docker)
export PATH := $(dir $(DOCKER)):$(PATH)
COMPOSE := $(DOCKER) compose
UV ?= uv

.PHONY: help sync up up-sim down clean lint fmt test test-integration soak reconcile

help:
	@grep -E '^[a-z-]+:' Makefile | cut -d: -f1

sync:
	$(UV) sync --frozen

up:  ## start core services (Postgres, Redpanda) and wait until healthy
	$(COMPOSE) --profile core up -d --wait

up-sim:  ## core + simulator containers
	$(COMPOSE) --profile core --profile sim up -d --build --wait

down:
	$(COMPOSE) --profile core --profile sim down

clean:  ## down and delete volumes (TMS data, topics, ground truth, checkpoints)
	$(COMPOSE) --profile core --profile sim down -v

lint:
	$(UV) run ruff format --check .
	$(UV) run ruff check .
	$(UV) run mypy

fmt:
	$(UV) run ruff format .
	$(UV) run ruff check --fix .

test:
	$(UV) run pytest -m "not integration"

test-integration:  ## needs `make up`
	$(UV) run pytest -m integration

soak:
	@echo "make soak is implemented in M6" >&2; exit 2

reconcile:
	@echo "make reconcile is implemented in M5" >&2; exit 2
