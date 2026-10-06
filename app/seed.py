from sqlalchemy import select
from app.core.config import settings
from app.core.security import hash_password
from app.db import SessionLocal
from app.models import ADMIN, User

if __name__ == "__main__":
    with SessionLocal() as db:
        if not db.scalar(select(User.id).where(User.email == settings.admin_email)):
            db.add(User(email=settings.admin_email, password_hash=hash_password(settings.admin_password),
                        full_name="Administrator", role=ADMIN))
            db.commit()
            print("admin created:", settings.admin_email)
