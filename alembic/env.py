"""Alembic environment wired to application settings."""

import sys
from logging.config import fileConfig
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from alembic import context  # noqa: E402
from sqlalchemy import engine_from_config, pool  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.infrastructure.database.base import Base  # noqa: E402
from app.infrastructure.database.models import (  # noqa: E402,F401
    DocumentModel,
    InvoiceItemModel,
    InvoiceModel,
    ProcessingJobModel,
    SupplierModel,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# DATABASE_URL comes exclusively from the environment (.env / compose).
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit SQL without a DBAPI connection (--sql mode)."""
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
