from alembic import context
from meridian import (
    analytics_models,  # noqa: F401
    compensation_models,  # noqa: F401
    finance_models,  # noqa: F401
    ingestion_models,  # noqa: F401
    models,  # noqa: F401
    operations_models,  # noqa: F401
    people_models,  # noqa: F401
    replay_models,  # noqa: F401
    scenario_models,  # noqa: F401
    talent_models,  # noqa: F401
)
from meridian.config import settings
from meridian.db import Base
from sqlalchemy import create_engine, pool

config = context.config
target_metadata = Base.metadata

if context.is_offline_mode():
    context.configure(
        url=settings().database_url, target_metadata=target_metadata, literal_binds=True
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = create_engine(settings().database_url, poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()
