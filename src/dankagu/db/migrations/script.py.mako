"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Migration authoring rules for this repository (see src/dankagu/db/README.md):

* expand -> migrate -> contract: never drop/rename a column in the same release
  that stops using it.
* Every DDL statement that can wait on a lock runs with a ``lock_timeout``.
* Indexes on live tables are created ``CONCURRENTLY`` (``autocommit_block``).
* Foreign keys are added ``NOT VALID`` first, then ``VALIDATE CONSTRAINT``.
* Data backfills are batched and idempotent so a re-run is harmless.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
