from alembic import context
from sqlalchemy import create_engine
from app.core.config import settings
from app.models import Base

engine = create_engine(settings.database_url)
with engine.connect() as conn:
    context.configure(connection=conn, target_metadata=Base.metadata)
    with context.begin_transaction():
        context.run_migrations()
