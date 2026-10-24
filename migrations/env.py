from alembic import context

from scheduler.db import Database

db = Database(context.config.attributes.get("database_path"))
with db.engine.connect() as connection:
    connection.exec_driver_sql("BEGIN IMMEDIATE")
    context.configure(connection=connection, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()
db.engine.dispose()
