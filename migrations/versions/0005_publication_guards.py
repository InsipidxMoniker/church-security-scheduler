"""Harden publication lifecycle without rebuilding or replacing historical tables."""
from alembic import op

revision = "0005_publication_guards"
down_revision = "0004_admin_pin_options"


def upgrade():
    op.execute("""CREATE TRIGGER current_publication_irreversible BEFORE UPDATE OF current_published_version_id ON service_dates
    WHEN OLD.first_published_at IS NOT NULL AND (NEW.current_published_version_id IS NULL OR
      (SELECT publication_number FROM schedule_versions WHERE id=NEW.current_published_version_id) <
      (SELECT publication_number FROM schedule_versions WHERE id=OLD.current_published_version_id))
    BEGIN SELECT RAISE(ABORT,'cannot unpublish or roll back'); END""")
    op.execute("""CREATE TRIGGER publication_exact_slots BEFORE UPDATE OF status ON schedule_versions
    WHEN NEW.status='published' AND (SELECT count(*) FROM slots WHERE version_id=OLD.id)!=16
    BEGIN SELECT RAISE(ABORT,'publication requires sixteen slots'); END""")


def downgrade():
    raise RuntimeError("Publication safeguards are irreversible; restore a verified backup instead")
