#!/usr/bin/env bash
# Celmis restore (Stage 21). Counterpart to backup.sh.
#
# Usage:
#   ./scripts/restore.sh backups/pg_20260718T020000Z.dump backups/data_20260718T020000Z.tar.gz
#
# DANGER: drops + recreates the Postgres DB and overwrites the data
# volume. Stop the API first: docker compose stop api web
#
# Validation mode (restore into a scratch DB, verify, drop):
#   VALIDATE_ONLY=1 ./scripts/restore.sh backups/pg_....dump

set -euo pipefail

PG_DUMP_FILE="${1:?usage: restore.sh <pg_dump> [data_tar]}"
DATA_TAR="${2:-}"
PG_CONTAINER="${PG_CONTAINER:-celmis-postgres}"
API_CONTAINER="${API_CONTAINER:-celmis-api}"
PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-celmis}"

if [ "${VALIDATE_ONLY:-0}" = "1" ]; then
  SCRATCH="celmis_restore_check"
  echo "[restore] VALIDATE ONLY — restoring into scratch db $SCRATCH"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c "DROP DATABASE IF EXISTS $SCRATCH"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c "CREATE DATABASE $SCRATCH"
  docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$SCRATCH" --no-owner < "$PG_DUMP_FILE"
  TABLES=$(docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$SCRATCH" -tAc \
    "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'")
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c "DROP DATABASE $SCRATCH"
  echo "[restore] validation OK — $TABLES tables restored into scratch db"
  exit 0
fi

echo "[restore] THIS WILL OVERWRITE $PG_DB — Ctrl+C within 5s to abort"
sleep 5

# 1) Postgres
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$PG_DB' AND pid <> pg_backend_pid()"
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c "DROP DATABASE IF EXISTS $PG_DB"
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -c "CREATE DATABASE $PG_DB"
docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner < "$PG_DUMP_FILE"
echo "[restore] postgres restored"

# 2) Data volume
if [ -n "$DATA_TAR" ]; then
  docker exec -i "$API_CONTAINER" sh -c 'cd / && tar xzf -' < "$DATA_TAR"
  echo "[restore] data volume restored"
fi

echo "[restore] done. Restart: docker compose up -d api web"
