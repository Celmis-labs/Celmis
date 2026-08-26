#!/usr/bin/env bash
# Celmis backup (Stage 21).
#
# Backs up:
#   1. Postgres — pg_dump custom format (projects, chats, policies, queue,
#      workspaces, oauth, compliance, ownership, deprecations)
#   2. data/ volume — SQLite stores (users, credentials, review_runs),
#      audit logs, vault MD, graphs (FalkorDBLite), clones EXCLUDED
#      (recoverable from git remotes; huge)
#
# Retention: keeps CELMIS_BACKUP_KEEP (default 14) most-recent sets.
#
# Usage:
#   ./scripts/backup.sh                       # to ./backups
#   BACKUP_DIR=/mnt/nas/celmis ./scripts/backup.sh
#
# Restore: see scripts/restore.sh + docs/BACKUP_RESTORE.md

set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-./backups}"
KEEP="${CELMIS_BACKUP_KEEP:-14}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
PG_CONTAINER="${PG_CONTAINER:-celmis-postgres}"
API_CONTAINER="${API_CONTAINER:-celmis-api}"
PG_USER="${POSTGRES_USER:-postgres}"
PG_DB="${POSTGRES_DB:-celmis}"

mkdir -p "$BACKUP_DIR"

echo "[backup] $STAMP starting → $BACKUP_DIR"

# 1) Postgres dump (custom format — supports pg_restore selective restore)
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -Fc "$PG_DB" \
  > "$BACKUP_DIR/pg_${STAMP}.dump"
echo "[backup] postgres → pg_${STAMP}.dump ($(du -h "$BACKUP_DIR/pg_${STAMP}.dump" | cut -f1))"

# 2) data volume tar (SQLite + audit + vault + graphs; exclude clones)
docker exec "$API_CONTAINER" tar czf - \
  --exclude='clones' --exclude='*.tmp' \
  -C /workspace data 2>/dev/null \
  > "$BACKUP_DIR/data_${STAMP}.tar.gz" || {
    # Fallback path layout — some deploys mount at /workspace/data directly
    docker exec "$API_CONTAINER" sh -c \
      'cd / && tar czf - --exclude="clones" workspace/data' \
      > "$BACKUP_DIR/data_${STAMP}.tar.gz"
  }
echo "[backup] data volume → data_${STAMP}.tar.gz ($(du -h "$BACKUP_DIR/data_${STAMP}.tar.gz" | cut -f1))"

# 3) Verify the pg dump is readable
docker exec -i "$PG_CONTAINER" pg_restore --list < "$BACKUP_DIR/pg_${STAMP}.dump" > /dev/null \
  && echo "[backup] pg dump verified"

# 4) Retention — delete oldest beyond KEEP sets
ls -1t "$BACKUP_DIR"/pg_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
ls -1t "$BACKUP_DIR"/data_*.tar.gz 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f

echo "[backup] done — $(ls -1 "$BACKUP_DIR" | wc -l | tr -d ' ') files retained"
