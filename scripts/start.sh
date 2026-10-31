#!/bin/sh
set -eu

exec /opt/venv/bin/uvicorn \
    app.main:create_app \
    --factory \
    --no-access-log \
    --host 0.0.0.0 \
    --port 8000 \
    "$@"
