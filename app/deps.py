from fastapi import Depends, Query
from fastapi.security import HTTPBearer
from app.core.errors import AppError
from app.core.security import decode_token
from app.db import get_db
from app.models import User

bearer = HTTPBearer(auto_error=False)


def current_user(creds=Depends(bearer), db=Depends(get_db)) -> User:
    if not creds:
        raise AppError("UNAUTHORIZED", "Authentication required", 401)
    payload = decode_token(creds.credentials, "access")
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise AppError("UNAUTHORIZED", "User not found or inactive", 401)
    return user


def require_roles(*roles):
    def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise AppError("FORBIDDEN", "Your role may not perform this action", 403)
        return user
    return dep


def pager(page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100)):
    return page, page_size
