#!/usr/bin/env bash
# Runs the backend integration tests against a throwaway local PostgreSQL.
# Needs: PostgreSQL binaries (initdb, pg_ctl) on PATH and Python 3.12+.
set -euo pipefail
export LC_ALL=C LANG=C   # PostgreSQL refuses to start with an unset/invalid locale on macOS
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
PORT="${PGPORT_TEST:-55432}"
trap 'pg_ctl -D "$TMP/data" -m immediate stop >/dev/null 2>&1 || true; rm -rf "$TMP"' EXIT

echo "test-password" > "$TMP/pw"
initdb -D "$TMP/data" -U postgres --locale=C --encoding=UTF8 --auth=scram-sha-256 --pwfile="$TMP/pw" >/dev/null
pg_ctl -D "$TMP/data" -o "-p $PORT -k $TMP -c listen_addresses=localhost" -l "$TMP/log" -w start >/dev/null
PGPASSWORD=test-password createdb -h localhost -p "$PORT" -U postgres exams

export INTEGRATION_DB=1 DB_HOST=localhost DB_PORT="$PORT" DB_NAME=exams DB_USER=postgres \
       DB_PASSWORD=test-password DB_TLS=false PYTHONDONTWRITEBYTECODE=1
python3 -m pytest "$ROOT/backend/tests" -q -p no:cacheprovider "$@"
