"""Encrypted PIN display copies for explicit Administrator viewing."""
from alembic import op

revision = "0004_admin_pin_options"
down_revision = "0003_current_link_display"


def upgrade():
    # Existing Argon2id hashes remain valid and cannot be reversed/backfilled.
    op.get_bind().exec_driver_sql(
        "CREATE TABLE volunteer_pin_secrets ("
        "volunteer_id TEXT PRIMARY KEY NOT NULL REFERENCES volunteers(id) ON DELETE RESTRICT,"
        "pin_ciphertext TEXT NOT NULL CHECK(length(pin_ciphertext)>0))"
    )


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup")
