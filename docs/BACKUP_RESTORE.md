# Backup & Restore Runbook (Stage 21)

## What gets backed up
| Store | Contents | Tool |
|---|---|---|
| Postgres | projects, chats, policies, sync_jobs, workspaces, oauth, compliance, ownership, deprecations | `pg_dump -Fc` |
| data/ volume | SQLite (users, credentials, review_runs), audit JSONL, vault MD, FalkorDBLite graphs | `tar czf` (clones excluded — recoverable from git remotes) |

## Nightly backup
```
0 2 * * * cd /path/to/celmis && BACKUP_DIR=/mnt/backups/celmis ./scripts/backup.sh >> /var/log/celmis-backup.log 2>&1
```
Retention: `CELMIS_BACKUP_KEEP` (default 14 sets).

## Restore
```
docker compose stop api web
./scripts/restore.sh backups/pg_<STAMP>.dump backups/data_<STAMP>.tar.gz
docker compose up -d api web
docker exec celmis-api alembic upgrade head   # no-op if dump is current
```

## Validate a backup WITHOUT touching prod
```
VALIDATE_ONLY=1 ./scripts/restore.sh backups/pg_<STAMP>.dump
```
Restores into a scratch DB, counts tables, drops it.

## RPO / RTO
- RPO: 24h with the nightly cron (tighten by running backup.sh more often).
- RTO: ~5 min for Postgres + data volume on typical sizes.
