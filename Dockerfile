# LAN party seat registration: gunicorn + PostgreSQL (see docker-compose.yml)
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt "gunicorn>=27,<28"

COPY lanparty ./lanparty

# run unprivileged; custom themes are mounted at /data/themes
RUN useradd --system --uid 10001 --no-create-home --home-dir /app lanparty \
    && mkdir -p /data/themes && chown -R lanparty /data
USER lanparty

# number of gunicorn worker processes (gunicorn reads WEB_CONCURRENCY)
ENV WEB_CONCURRENCY=4 \
    THEMES_DIR=/data/themes

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/info', timeout=4)"]

CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--no-control-socket", "lanparty:create_app()"]
