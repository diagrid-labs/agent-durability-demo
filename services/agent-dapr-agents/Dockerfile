FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --no-cache-dir .

COPY . ./

EXPOSE 8000

CMD ["uvicorn", "agent_worker.main:app", "--host", "0.0.0.0", "--port", "8000"]
