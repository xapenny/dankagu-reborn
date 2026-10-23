# Versioned database migrations

The database schema is versioned with [Alembic](https://alembic.sqlalchemy.org/).
`alembic_version` holds the single revision the database is at; every schema
change is a revision file under `src/dankagu/db/migrations/versions/`.

This replaced the old "run `Base.metadata.create_all` at startup and patch missing
columns with `ALTER TABLE ... IF NOT EXISTS`" approach, which could create a
schema but never evolve one safely.

## How it runs

| Situation | What happens |
|---|---|
| Server start, `DANKAGU_DB_AUTO_MIGRATE=true` (default) | `run_migrations()` runs on one advisory-locked connection **before any listener binds**. Failure aborts startup on purpose. |
| Server start, `DANKAGU_DB_AUTO_MIGRATE=false` | Migrations are skipped; do them in the deployment step instead (`python -m dankagu.db.migrate --upgrade`). |
| Database created **before** Alembic existed (application tables, no `alembic_version`) | It is **stamped** with `0001_baseline`: no DDL is replayed, no player data is touched. |
| Empty database | `upgrade head` creates the full schema from `0001_baseline`. |
| Several instances start at once | The PostgreSQL advisory lock (`0x444B4D47`) serialises them; the loser waits up to `DANKAGU_DB_MIGRATE_WAIT_SECONDS` (default 300). |

## Everyday commands

```bash
python -m dankagu.db.migrate --upgrade          # default: upgrade to head (locked, bootstraps legacy DBs)
python -m dankagu.db.migrate --current          # applied revision
python -m dankagu.db.migrate --history          # full revision history
python -m dankagu.db.migrate --check            # exit 1 when models drifted from the revisions (CI)
python -m dankagu.db.migrate --downgrade -1     # roll back one revision
python -m dankagu.db.migrate --stamp 0001_baseline   # record a revision without running it

# Equivalent, straight from Alembic (used by CI):
uv run alembic upgrade head
uv run alembic check
```

## Adding a revision

```bash
uv run alembic revision --autogenerate -m "add players.is_searchable"
# or, for a pure data/backfill revision:
uv run alembic revision -m "normalize friend relation ids"
```

Always review the generated file: autogenerate describes what the ORM metadata
changed, not what is safe to run against a live database. `uv run alembic check`
(and CI) fails when the models and the revisions disagree, so a model change
without a revision cannot slip through.

## Rules for revisions (a live server depends on these)

1. **expand → migrate → contract.** Add the new column/table and keep writing to
   the old one; migrate data; drop the old one in a *later* release. Never
   `DROP COLUMN`/rename in the same release that stops using it, because the
   previous application version is still running during a rolling deploy.
2. **Bounded lock waits.** Every DDL revision relies on the runner's
   `lock_timeout` (15 s). A statement that would block longer must be rewritten
   (batched), not waited out.
3. **Indexes on live tables use `CREATE INDEX CONCURRENTLY`** inside
   `with op.get_context().autocommit_block():` — `CONCURRENTLY` cannot run inside
   a transaction.
4. **Foreign keys go in `NOT VALID` first**, then `ALTER TABLE ... VALIDATE
   CONSTRAINT` in a separate revision/statement. Adding one directly takes a
   blocking scan.
5. **Backfills are batched and idempotent** (`WHERE ... LIMIT n` loop in Python,
   or `ctid` ranges), so a re-run after a failure is harmless and the transaction
   stays short.
6. **`0001_baseline` is frozen.** Existing databases are stamped with it, so it
   must keep describing the original schema; express every change as a new
   revision.
7. **Every revision implements `downgrade()`.** Data changes that cannot be
   reversed automatically must say so in the docstring and restore from a backup
   table (`..._backup_<date>`) instead of pretending.

## Revision history

| Revision | What it does |
|---|---|
| `0001_baseline` | The complete schema as it existed before Alembic (24 tables) plus `player_short_id_seq`. Existing databases are *stamped* with it, never re-created. |
| `0002_friend_relations_normalize` | Repairs `friend_relations`: rewrites `short_id`/`user_id` references to `players.id`, drops duplicates, quarantines rows whose player no longer exists, then adds the `ON DELETE CASCADE` foreign keys. |

### Audit tables written by `0002`

These are deliberately **not** ORM models and are excluded from autogenerate by
`dankagu.db.schema_scope` (otherwise `alembic check` would report them as removed
tables). They survive a rollback so an operator can always see what happened:

```sql
-- what the revision changed
SELECT action, column_name, count(*) FROM friend_relations_repair_log GROUP BY 1, 2;
-- rows whose player no longer exists (kept out of the live table)
SELECT * FROM friend_relations_orphans;
-- full pre-revision snapshot, for manual recovery of deleted duplicates
SELECT * FROM friend_relations_backup_0002;
```

`downgrade()` restores the quarantine and the rewritten reference strings
automatically. Deleted duplicates are **not** re-inserted (that would violate
`uq_friend_follower_target`, which the application relies on) - restore them from
the snapshot if a duplicate really has to come back.

## Wiring a new setting

`alembic.ini` deliberately has no database URL: `env.py` injects
`settings.db_url`, so credentials stay in `.env` (`DANKAGU_DB_URL`). Two settings
control the runner:

| Setting | Default | Meaning |
|---|---|---|
| `DANKAGU_DB_AUTO_MIGRATE` | `true` | Migrate during startup. |
| `DANKAGU_DB_MIGRATE_WAIT_SECONDS` | `300` | How long to wait for the advisory lock before failing. |

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `MigrationLockTimeout` on start | Another instance is still migrating. Wait, or raise `DANKAGU_DB_MIGRATE_WAIT_SECONDS`. Check `SELECT * FROM pg_locks WHERE locktype='advisory';`. |
| `alembic check` reports tables/columns missing | A model changed without a revision: create one. |
| `alembic check` reports extra objects in the database | Someone applied DDL by hand. Either add a revision describing it (so every environment converges) or drop the object. |
| Startup fails after a revision was added | By design. Fix the revision (`--downgrade -1` if it partially applied — DDL is transactional in PostgreSQL, so a failed revision left the previous one intact and only the data backfill may need a re-run). |
| Legacy database must be adopted without migrating | `python -m dankagu.db.migrate --stamp 0001_baseline` then `--upgrade`. |

## Tests

`tests/test_db_migrations.py` covers the three paths that matter in production:
fresh install, legacy adoption (with data intact), and downgrade/upgrade
round-trip. It creates and drops its own scratch database
(`dankagu_migrations_test`) and never touches the servers' databases.
