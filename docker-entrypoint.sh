#!/bin/sh
# Runs schema migrations before the service starts, then execs the service CMD.
set -e

echo "[entrypoint] applying database migrations: alembic upgrade head"
alembic upgrade head

exec "$@"
