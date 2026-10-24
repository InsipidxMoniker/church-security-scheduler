"""Public roster disambiguation and bounded Unicode search for Phase 2."""
from alembic import op

revision = "0002_volunteer_flow"
down_revision = "0001_foundation"


def upgrade():
    c = op.get_bind()
    c.exec_driver_sql("ALTER TABLE volunteers ADD COLUMN public_label TEXT NOT NULL DEFAULT ''")
    c.exec_driver_sql("ALTER TABLE volunteers ADD COLUMN search_text TEXT NOT NULL DEFAULT ''")
    for person in c.exec_driver_sql("SELECT id,display_name FROM volunteers").mappings().all():
        label = person["display_name"].strip()
        c.exec_driver_sql("UPDATE volunteers SET public_label=?,search_text=? WHERE id=?", (label, label.casefold(), person["id"]))
    c.exec_driver_sql("CREATE INDEX roster_page ON volunteers(active,normalized_label,id)")
    c.exec_driver_sql("CREATE INDEX link_creator ON date_links(creator)")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup")
