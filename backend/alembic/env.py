"""Alembic environment for Trajecta.

Schema is authored as SQLAlchemy declarative models in
``backend.app.models``. ``Base.metadata`` is the autogenerate source.

The DB URL resolves at runtime from the same place the app uses
(``backend.app.db._db_path``), so ``alembic upgrade head`` operates on the
real ``data/trajecta.db`` whether you are running locally or in CI.
"""

from __future__ import annotations

import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool

from alembic import context


# Make the project importable when alembic is run from backend/.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.app import db as _db  # noqa: E402
from backend.app.models import Base  # noqa: E402


config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the static URL with the runtime-resolved one so devs do not
# have to edit alembic.ini per environment.
config.set_main_option("sqlalchemy.url", f"sqlite:///{_db._db_path()}")

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
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
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
