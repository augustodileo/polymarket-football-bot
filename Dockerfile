FROM python:3.12-slim

WORKDIR /app

# Version injected from git tag at build time
ARG VERSION=dev
RUN echo "${VERSION}" > VERSION

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (cache layer)
COPY pyproject.toml uv.lock* ./

# Install dependencies
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# Copy source code only — NO config.yaml
COPY src/ ./src/

# Data + config mount points
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# Disable Python output buffering so print() shows in docker logs
ENV PYTHONUNBUFFERED=1

# config.yaml must be provided at runtime via volume mount
ENTRYPOINT ["uv", "run", "python", "src/main.py"]
CMD ["--paper"]
