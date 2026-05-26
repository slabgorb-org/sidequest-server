"""Alembic migration environment (ADR-115).

Resolves the connection URL from sidequest.game.db_config (single source),
runs migrations online against a real Postgres. We author raw SQL in each
revision via op.execute(...); there is no SQLAlchemy model metadata and no
autogenerate, so target_metadata stays None.
"""

from __future__ import annotations

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context
from sidequest.game.db_config import alembic_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# A URL set programmatically (tests pass one via set_main_option) wins;
# otherwise resolve from the environment. No silent localhost default.
if not config.get_main_option("sqlalchemy.url"):
    config.set_main_option("sqlalchemy.url", alembic_url())

target_metadata = None


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
