#!/bin/sh
# Runs once on first start: a separate database for Prefect's state.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
	CREATE DATABASE prefect OWNER "$POSTGRES_USER";
SQL
