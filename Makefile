# Docker must be on PATH (Docker Desktop on macOS adds ~/.docker/bin via ~/.zprofile).
# Last-resort fallback for non-login macOS shells only; it never applies on Linux.
DOCKER := $(shell command -v docker 2>/dev/null)
ifeq ($(DOCKER),)
  ifeq ($(shell uname -s),Darwin)
    DOCKER := /Applications/Docker.app/Contents/Resources/bin/docker
    export PATH := $(dir $(DOCKER)):$(PATH)
    $(warning docker not on PATH; falling back to $(DOCKER). Add ~/.docker/bin to PATH.)
  else
    DOCKER = $(error docker not found on PATH)
  endif
endif
COMPOSE = $(DOCKER) compose  # recursive: the Linux error fires only for docker targets
UV ?= uv

.PHONY: help sync up up-sim down clean migrate migrate-down seed lint fmt test test-integration soak reconcile

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

migrate:  ## apply TMS migrations (needs `make up`)
	$(UV) run alembic upgrade head

migrate-down:  ## roll the TMS schema back to empty
	$(UV) run alembic downgrade base

PRESET ?= s
seed:  ## idempotent master-data seed, e.g. `make seed PRESET=m`
	$(UV) run vlsim seed --preset $(PRESET)

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
