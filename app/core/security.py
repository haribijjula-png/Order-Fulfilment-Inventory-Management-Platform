import uuid
from datetime import datetime, timedelta, timezone
import bcrypt
import jwt
from app.core.config import settings
from app.core.errors import AppError


def hash_password(p: str) -> str:
    return bcrypt.hashpw(p.encode(), bcrypt.gensalt(settings.bcrypt_rounds)).decode()


def verify_password(p: str, h: str) -> bool:
    return bcrypt.checkpw(p.encode(), h.encode())


def make_token(user_id: int, typ: str, delta: timedelta | None = None):
    delta = delta or (timedelta(minutes=settings.access_token_minutes) if typ == "access"
                      else timedelta(days=settings.refresh_token_days))
    exp, jti = datetime.now(timezone.utc) + delta, uuid.uuid4().hex
    token = jwt.encode({"sub": str(user_id), "type": typ, "jti": jti, "exp": exp}, settings.secret_key, "HS256")
    return token, jti, exp


def decode_token(token: str, typ: str) -> dict:
    try:
        p = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise AppError("TOKEN_EXPIRED", "Token has expired", 401)
    except jwt.InvalidTokenError:
        raise AppError("INVALID_TOKEN", "Invalid token", 401)
    if p.get("type") != typ:
        raise AppError("INVALID_TOKEN", f"Expected a {typ} token", 401)
    return p
