"""Scope of the ORM metadata that Alembic autogenerate may compare.

Data-repair revisions create audit tables (a change log, a quarantine table, a
pre-migration snapshot). They are deliberately **not** ORM models: no application
code reads them, and they must survive independently of the schema. Autogenerate
would otherwise report them as "removed tables" on every subsequent run and make
``alembic check`` fail.

Keeping the filter here means exactly one definition of "schema drift" is used by
``env.py`` (and therefore by the ``alembic`` CLI, ``dankagu.db.migrate --check``
and CI).
"""

from __future__ import annotations

from typing import Any

#: Table-name prefixes that exist in the database but not in the ORM metadata.
#: The trailing underscore on ``..._backup_`` is intentional: the name carries a
#: date/revision suffix, e.g. ``friend_relations_backup_0002``.
UNMANAGED_TABLE_PREFIXES: tuple[str, ...] = (
    "friend_relations_repair_log",
    "friend_relations_orphans",
    "friend_relations_backup_",
)


def _table_name_of(obj: Any, name: str | None, type_: str) -> str | None:
    if type_ == "table":
        return name
    table = getattr(obj, "table", None)
    return getattr(table, "name", None)


def include_object(
    obj: Any,
    name: str | None,
    type_: str,
    reflected: bool,
    compare_to: Any,
) -> bool:
    """Alembic ``include_object`` hook: hide audit tables from autogenerate."""
    table = _table_name_of(obj, name, type_)
    if table and table.startswith(UNMANAGED_TABLE_PREFIXES):
        return False
    return True
