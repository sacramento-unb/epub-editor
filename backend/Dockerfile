# API e worker (mesma imagem). Python 3.12 + Tesseract (por/eng) + Java (EPUBCheck) + Chromium (Playwright).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/ms-playwright EPUBCHECK_JAR=/opt/epubcheck/epubcheck.jar

ARG EPUBCHECK_VERSION=5.4.0
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr tesseract-ocr-por tesseract-ocr-eng default-jre-headless \
        fonts-dejavu-core fonts-liberation curl unzip ca-certificates \
    && curl -fsSL -o /tmp/epubcheck.zip \
        "https://github.com/w3c/epubcheck/releases/download/v${EPUBCHECK_VERSION}/epubcheck-${EPUBCHECK_VERSION}.zip" \
    && unzip -q /tmp/epubcheck.zip -d /opt && mv /opt/epubcheck-${EPUBCHECK_VERSION} /opt/epubcheck && rm /tmp/epubcheck.zip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown app:app /data
USER app
ENV DATA_DIR=/data
EXPOSE 8000
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
