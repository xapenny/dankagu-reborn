"""purchase_receipts: make a repeated ``transaction_id`` a replay, not a purchase.

The client resends a purchase when it never sees the answer. ``PurchaseV1`` charged
and rolled again on every delivery, so a retry either bought an extra pull or charged
twice for one pull whose answer was lost. Idempotency needs somewhere to remember
what was already accepted.

This revision only adds the table; the charge and the receipt are written in the same
transaction, so a crash leaves neither behind.

Both steps tolerate an existing object on purpose. A database created before Alembic
existed is *stamped* at the baseline rather than rebuilt, so a build whose
``create_all`` already produced this table (exactly what ``init_db`` does) would
otherwise fail the revision with "relation already exists".

``downgrade()`` drops it; the rows are bookkeeping, and after a downgrade a repeated
purchase is simply treated as a new purchase again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_purchase_receipts"
down_revision: str | None = "0003_transfer_code_lifetime"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "purchase_receipts"
_INDEX = "ix_purchase_receipts_transaction_id"


def _has_table(name: str) -> bool:
    return name in sa.inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    if not _has_table(table):
        return False
    return any(index.get("name") == name for index in sa.inspect(op.get_bind()).get_indexes(table))


def upgrade() -> None:
    if not _has_table(_TABLE):
        op.create_table(
            _TABLE,
            sa.Column("player_id", sa.String(length=64), nullable=False),
            sa.Column("transaction_id", sa.String(length=128), nullable=False),
            sa.Column("product_id", sa.String(length=128), nullable=False),
            sa.Column("card_ids", sa.String(length=2048), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.ForeignKeyConstraint(["player_id"], ["players.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("player_id", "transaction_id"),
        )

    if not _has_index(_TABLE, _INDEX):
        op.create_index(_INDEX, _TABLE, ["transaction_id"], unique=False)


def downgrade() -> None:
    if _has_index(_TABLE, _INDEX):
        op.drop_index(_INDEX, table_name=_TABLE)
    if _has_table(_TABLE):
        op.drop_table(_TABLE)
