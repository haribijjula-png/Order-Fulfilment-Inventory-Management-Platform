"""initial schema + append-only triggers"""
from alembic import op
from app.models import Base

revision = "0001"
down_revision = None
APPEND_ONLY = ("inventory_transactions", "audit_logs")


def upgrade():
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)
    if bind.dialect.name == "postgresql":
        op.execute("""CREATE OR REPLACE FUNCTION forbid_mutation() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION '% is append-only', TG_TABLE_NAME; END; $$ LANGUAGE plpgsql""")
        for t in APPEND_ONLY:
            op.execute(f"CREATE TRIGGER {t}_immutable BEFORE UPDATE OR DELETE ON {t} "
                       f"FOR EACH ROW EXECUTE FUNCTION forbid_mutation()")


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for t in APPEND_ONLY:
            op.execute(f"DROP TRIGGER IF EXISTS {t}_immutable ON {t}")
        op.execute("DROP FUNCTION IF EXISTS forbid_mutation()")
    Base.metadata.drop_all(bind=bind)
