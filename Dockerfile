FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip && pip install .

RUN mkdir -p /app/data /app/reports \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# The container listens on all interfaces; exposure is controlled by the
# compose port binding (loopback) and the mandatory API key.
CMD ["uvicorn", "--factory", "engram.api:get_app", "--host", "0.0.0.0", "--port", "8000"]
