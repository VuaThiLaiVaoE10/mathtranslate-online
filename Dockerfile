FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -u 1000 appuser
WORKDIR /app

COPY pyproject.toml README.md* ./
COPY mathtranslate ./mathtranslate
COPY web ./web
COPY gui ./gui

RUN pip install --no-cache-dir -e ".[web,pdf]" \
    && chown -R appuser:appuser /app

USER appuser
ENV MATHTRANSLATE_HOST=0.0.0.0 \
    PORT=7860 \
    PYTHONUNBUFFERED=1

EXPOSE 7860
CMD ["mathtranslate-web"]
