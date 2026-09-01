FROM ghcr.io/astral-sh/uv:0.7.17-python3.12-bookworm-slim

WORKDIR /workspace
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/ingestion-venv \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-spa \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md uv.lock ./
RUN uv sync --no-dev --frozen

COPY src ./src
COPY configs ./configs

ENTRYPOINT ["uv", "run", "ingestion"]
