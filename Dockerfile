# LAN party seat registration: gunicorn + PostgreSQL (see docker-compose.yml)
# pinned by digest (Dependabot proposes updates); rebuilt with every release
FROM python:3.13-slim@sha256:7249d5587b78209a006dc4a3b5340678f84ea729239193aa559962cd8c1fce50

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt requirements-docker.txt ./
RUN pip install -r requirements-docker.txt

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
