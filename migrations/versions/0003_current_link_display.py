"""Encrypted current shared link for authenticated staff redisplay."""
from alembic import op

revision = "0003_current_link_display"
down_revision = "0002_volunteer_flow"


def upgrade():
    # Existing digests remain valid but cannot be reversed/backfilled.
    op.get_bind().exec_driver_sql(
        "CREATE TABLE date_link_secrets ("
        "link_id TEXT PRIMARY KEY NOT NULL REFERENCES date_links(id) ON DELETE RESTRICT,"
        "token_ciphertext TEXT NOT NULL CHECK(length(token_ciphertext)>0))"
    )


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup")
