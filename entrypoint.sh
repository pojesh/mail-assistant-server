#!/bin/sh
# Run database migrations (Alembic takes an advisory lock, so parallel starts are safe),
# then start the API server.
set -e
alembic upgrade head
# --no-access-log: access lines would contain query strings (e.g. the OAuth code).
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips="*" --no-access-log
