FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy HF_HOME=/models
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY timesfm_serve ./timesfm_serve
COPY scripts ./scripts

ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "timesfm_serve.api:app", "--host", "0.0.0.0", "--port", "8000"]
