from alembic import context
from sqlalchemy import create_engine, pool
from src.core.config import get_settings
from src.models import Base


def run():
    if context.is_offline_mode():
        context.configure(url=get_settings().db_url_sync, target_metadata=Base.metadata, literal_binds=True)
        with context.begin_transaction():
            context.run_migrations()
    else:
        engine = create_engine(get_settings().db_url_sync, poolclass=pool.NullPool)
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
        engine.dispose()

run()
