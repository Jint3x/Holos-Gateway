# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

FROM python:3.14-slim-trixie@sha256:cd89c30eda26ee911a4611b18680086e0ea42e6f5f8cbe653d07fa7958c9cec8 AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH"
WORKDIR /app
RUN groupadd --gid 10001 gateway \
    && useradd --uid 10001 --gid gateway --create-home gateway
COPY --chmod=755 scripts/start.sh /app/scripts/start.sh
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:8000/', timeout=2).close()"]
CMD ["/app/scripts/start.sh"]

FROM base AS dependencies
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy
COPY pyproject.toml uv.lock .python-version ./

FROM dependencies AS production-dependencies
# Optional trust bundle for builds behind an HTTPS proxy; never stored in a layer.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-dev --no-install-project

FROM dependencies AS development
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then export SSL_CERT_FILE=/run/secrets/proxy_ca; fi; \
    uv sync --locked --no-install-project
ENV UV_CACHE_DIR=/tmp/uv-cache \
    RUFF_CACHE_DIR=/tmp/ruff-cache \
    PYTEST_ADDOPTS="-o cache_dir=/tmp/pytest-cache"
COPY --chown=gateway:gateway --chmod=u=rwX,go=rX . /app
RUN chown gateway:gateway /app
USER gateway
CMD ["/app/scripts/start.sh", "--reload"]

FROM base AS production
COPY --from=production-dependencies /opt/venv /opt/venv
COPY --chmod=u=rwX,go=rX app ./app
USER gateway
