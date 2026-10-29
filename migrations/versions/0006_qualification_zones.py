"""Rename eligibility in place; add dependent rifle qualification and zone labels."""
from alembic import op

revision = "0006_qualification_zones"
down_revision = "0005_publication_guards"


def upgrade():
    # Native rename preserves UUIDs, references, indexes, triggers and credentials.
    # SQLite validates the new CHECK against every existing row (default false).
    op.execute("ALTER TABLE volunteers RENAME COLUMN restricted_qualified TO outside_available")
    op.execute("ALTER TABLE volunteers ADD COLUMN rifle_qualified INTEGER NOT NULL DEFAULT 0 CHECK(rifle_qualified IN (0,1) AND (rifle_qualified=0 OR outside_available=1))")
    op.execute("ALTER TABLE settings ADD COLUMN inside_zone_label TEXT NOT NULL DEFAULT 'Inside'")
    op.execute("ALTER TABLE settings ADD COLUMN outside_zone_label TEXT NOT NULL DEFAULT 'Outside'")
    # No publication/config payload is rewritten. Existing drafts need the normal
    # explicit diff/refresh to adopt labels; new drafts capture the new settings.
    op.execute("UPDATE settings SET config_epoch=config_epoch+1,settings_revision=settings_revision+1 WHERE EXISTS (SELECT 1 FROM schedule_versions WHERE status='draft')")


def downgrade():
    raise RuntimeError("Restore a verified pre-migration backup with its matching application version")
