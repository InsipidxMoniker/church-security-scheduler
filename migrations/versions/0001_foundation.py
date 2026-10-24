"""Foundation schema, including integrity boundaries for later workflows."""
import sqlite3
from pathlib import Path

from alembic import op

revision = "0001_foundation"
down_revision = None


def upgrade():
    statement = ""
    for line in Path(__file__).with_name("0001_schema.sql").read_text().splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            op.get_bind().exec_driver_sql(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("Incomplete migration")


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore a verified backup")
