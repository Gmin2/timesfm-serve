FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

# cpu for local and ci, gpu for the cuda node
ARG TORCH=cpu
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy HF_HOME=/models
WORKDIR /app

# bake the checkpoint in first so code and lock changes never redownload it.
# the cache mount keeps the 1.3gb download across builds on the same machine.
RUN --mount=type=cache,target=/hf-cache \
    uv pip install --system huggingface_hub \
    && HF_HOME=/hf-cache python -c "from huggingface_hub import snapshot_download; snapshot_download('google/timesfm-3.0-pytorch')" \
    && mkdir -p /models/hub && cp -r /hf-cache/hub/models--google--timesfm-3.0-pytorch /models/hub/

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev --extra $TORCH

COPY inference ./inference
COPY timesfm_serve ./timesfm_serve
COPY scripts ./scripts
COPY data ./data

ENV PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app
EXPOSE 8000
CMD ["uvicorn", "timesfm_serve.api:app", "--host", "0.0.0.0", "--port", "8000"]
