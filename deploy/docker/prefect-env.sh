#!/bin/sh
# Build Prefect's DB URL from the password secret without putting it in the compose file.
set -eu
PREFECT_API_DATABASE_CONNECTION_URL="postgresql+asyncpg://kagebox:$(cat /run/secrets/db_password)@postgres:5432/prefect"
export PREFECT_API_DATABASE_CONNECTION_URL
exec "$@"
