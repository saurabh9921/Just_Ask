# syntax=docker/dockerfile:1.7
# Build context: repository root  (docker build -f apps/api/Dockerfile .)

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PATH="/opt/venv/bin:${PATH}"

# ---------------------------------------------------------------- builder
FROM base AS builder
RUN python -m venv /opt/venv
WORKDIR /build
COPY apps/api/pyproject.toml apps/api/README.md ./
COPY apps/api/app ./app
RUN pip install ".[oracle]"

# ---------------------------------------------------------------- runtime
FROM base AS runtime
# unixODBC + Microsoft ODBC Driver 18 for the SQL Server connector (aioodbc/pyodbc).
# Build with --build-arg INSTALL_MSODBC=0 to omit it (the connector then reports
# CONNECTOR_NOT_AVAILABLE). Accepting the driver EULA is a condition of installing it.
# packages-microsoft-prod.deb installs Microsoft's current signing key and repository
# for the image's Debian release (the old microsoft.asc key fails on Debian 13).
ARG INSTALL_MSODBC=1
RUN if [ "$INSTALL_MSODBC" = "1" ]; then \
      apt-get update \
      && apt-get install -y --no-install-recommends curl ca-certificates unixodbc \
      && . /etc/os-release \
      && curl -fsSL -o /tmp/packages-microsoft-prod.deb \
         "https://packages.microsoft.com/config/debian/${VERSION_ID}/packages-microsoft-prod.deb" \
      && dpkg -i /tmp/packages-microsoft-prod.deb \
      && rm /tmp/packages-microsoft-prod.deb \
      && apt-get update \
      && ACCEPT_EULA=Y apt-get install -y --no-install-recommends msodbcsql18 \
      && apt-get purge -y --auto-remove curl \
      && rm -rf /var/lib/apt/lists/*; \
    fi
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /srv --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
WORKDIR /srv
# Export files (shared volume with the worker; see docker-compose.yml)
RUN mkdir -p /srv/data/exports && chown -R 10001:10001 /srv/data
ENV EXPORT_DIR=/srv/data/exports
# Governed semantic layer (read-only in the image; mount over it to customise)
COPY --chown=root:root semantic-layer /srv/semantic-layer
ENV SEMANTIC_LAYER_PATH=/srv/semantic-layer
COPY --chown=root:root apps/api/alembic.ini ./alembic.ini
# Only trust X-Forwarded-* from these proxy addresses (override per deployment).
ENV FORWARDED_ALLOW_IPS=127.0.0.1
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--no-server-header", \
     "--timeout-graceful-shutdown", "20"]
