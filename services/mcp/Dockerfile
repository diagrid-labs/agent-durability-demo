FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Build context is the repo root so we can bake the UI into the same image.
# (Locally, compose bind-mounts ui-prototype/ instead — see local/compose.yaml.)
COPY services/mcp/pyproject.toml ./
RUN pip install --no-cache-dir .

COPY services/mcp/ ./
COPY ui-prototype/ /ui-prototype/

ENV UI_PROTOTYPE_DIR=/ui-prototype

EXPOSE 8000

CMD ["uvicorn", "mcp_server.server:app", "--host", "0.0.0.0", "--port", "8000"]
