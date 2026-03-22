FROM python:3.12-slim

WORKDIR /app

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy dependency files first (cache layer)
COPY pyproject.toml uv.lock* ./

# Install dependencies
RUN uv sync --no-dev --frozen 2>/dev/null || uv sync --no-dev

# Copy source code only — NO config.yaml
COPY main.py engine.py stats.py analyze.py ./

# Data + config mount points
RUN mkdir -p /app/data
VOLUME ["/app/data"]

# config.yaml must be provided at runtime via volume mount:
#   docker run -v /path/to/config.yaml:/app/config.yaml poly-bot --paper

ENTRYPOINT ["uv", "run", "python", "main.py"]
CMD ["--paper"]
