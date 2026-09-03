FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# cpu for local and ci, gpu for the cuda node
ARG TORCH=cpu
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy HF_HOME=/models
WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra $TORCH

COPY timesfm_serve ./timesfm_serve
COPY scripts ./scripts

ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "timesfm_serve.api:app", "--host", "0.0.0.0", "--port", "8000"]
