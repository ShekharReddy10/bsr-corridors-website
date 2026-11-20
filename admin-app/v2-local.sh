#!/usr/bin/env bash
# v2 development only: run manage.py against the PRIVATE local test database, never the live one.
#
#   ./v2-local.sh start                 start the local Postgres (port 5433) if it isn't running
#   ./v2-local.sh runserver             any manage.py command, e.g. migrate, test core, shell
#
# Overrides .env: DATABASE_URL → local test database; Google Drive keys emptied (no uploads to the real Drive).
set -euo pipefail
cd "$(dirname "$0")"

PG=/usr/lib/postgresql/18/bin
DATA="$HOME/.local/share/bsr-v2-pg"
export DATABASE_URL="postgres://bsr@localhost:5433/bsr_v2"
export GOOGLE_CLIENT_ID="" GOOGLE_CLIENT_SECRET="" GOOGLE_REFRESH_TOKEN=""
export DJANGO_DEBUG=1 SHORT_LINK_BASE=""

running() { "$PG/pg_ctl" -D "$DATA/data" status >/dev/null 2>&1; }

if [[ "${1:-}" == "start" ]]; then
  running || "$PG/pg_ctl" -D "$DATA/data" -o "-p 5433 -k $DATA -c listen_addresses=localhost" -l "$DATA/log.txt" start
  exit 0
fi
running || { echo "Local test database is not running — run: ./v2-local.sh start" >&2; exit 1; }

# Belt and braces: refuse anything that isn't the local test database.
case "$DATABASE_URL" in
  *@localhost:5433/bsr_v2) ;;
  *) echo "Refusing: DATABASE_URL is not the local test database." >&2; exit 1 ;;
esac

exec .venv/bin/python manage.py "$@"
