FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ARTIFACT_VAULT_DIR=/tmp/evidence_delta_artifacts

RUN apt-get update \
    && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src

RUN python -m pip install --no-cache-dir . \
    && useradd --create-home --uid 10001 evidence-delta \
    && mkdir -p /tmp/evidence_delta_artifacts \
    && chown -R evidence-delta:evidence-delta /tmp/evidence_delta_artifacts

USER evidence-delta

CMD ["sh", "-c", "uvicorn evidence_delta.api:app --host 0.0.0.0 --port ${PORT:-10000}"]
