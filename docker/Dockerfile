# One image, two entrypoints (sim-engine, tms-service). Base images are multi-arch.
FROM ghcr.io/astral-sh/uv:0.12.13 AS uv

FROM python:3.12.15-slim-bookworm
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH VLSIM_CONFIG_DIR=/app/config
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY config ./config
RUN uv sync --frozen --no-dev
RUN useradd --create-home --uid 10001 vlsim
USER vlsim
