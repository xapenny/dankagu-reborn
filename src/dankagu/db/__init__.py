"""Database schema versioning (Alembic).

The implementation lives in :mod:`dankagu.db.migrate`; import it explicitly::

    from dankagu.db import migrate

    await migrate.run_migrations()          # bootstrap a legacy DB, then upgrade
    await migrate.current_revision()        # applied revision
    migrate.check_models_match_migrations() # drift check (CI)

This package intentionally does not import the submodule here: doing so would
make ``python -m dankagu.db.migrate`` load it twice (once as a package attribute,
once as ``__main__``), which Python warns about.

CLI and operational documentation: ``src/dankagu/db/README.md``.
"""
