# =============================================================================
# Multi-stage image: builder compiles wheels; runtime stays slim.
# System requirement: tesseract-ocr binary for local OCR.
# The same image serves api / worker / streamlit (command differs per service).
# =============================================================================
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
RUN pip install --upgrade pip setuptools wheel
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip wheel --wheel-dir /wheels .

# -----------------------------------------------------------------------------
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000

# Tesseract engine + English/Spanish data + OpenCV/Paddle system libs
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      tesseract-ocr tesseract-ocr-eng tesseract-ocr-spa \
      libgl1 libglib2.0-0 libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -rf /wheels

# Local execution mode (LLM_EXECUTION=local): PP-OCR/PP-Structure +
# PaddleOCR-VL via paddleocr. Heavy wheels, but only the worker loads
# them at runtime (lazy import); api/streamlit never import paddle.
# The VL pipeline additionally requires the matching paddlex[ocr] extra.
RUN pip install --no-cache-dir paddlepaddle paddleocr \
 && PXV=$(python -c "import importlib.metadata as m; print(m.version('paddlex'))") \
 && pip install --no-cache-dir "paddlex[ocr]==${PXV}"

# Schema migrations travel with the image: api/worker run
# `alembic upgrade head` at startup via the entrypoint below.
COPY alembic.ini ./
COPY alembic ./alembic
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# Source tree needed by entrypoints that take FILE paths (streamlit run …)
# and keeps container code identical to the repo.
COPY app ./app
COPY scripts/healthcheck.py ./scripts/healthcheck.py

RUN useradd --create-home --uid 1000 appuser \
 && mkdir -p /app/data/uploads \
 && chown -R appuser:appuser /app

# Pre-download OCR/VL models at build time so the first local request
# does not pay the download. Best-effort: a failure here only means the
# models will download on first use instead.
USER appuser
RUN python -c "from paddleocr import PaddleOCRVL; PaddleOCRVL()" >/dev/null 2>&1 \
 && python -c "from paddleocr import PaddleOCR; PaddleOCR(lang='es')" >/dev/null 2>&1 \
 || echo "WARNING: model warm-up skipped; models will download on first use"
USER root

EXPOSE 8000 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=25s --retries=3 \
  CMD ["python", "/app/scripts/healthcheck.py"]

CMD ["uvicorn", "app.presentation.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
