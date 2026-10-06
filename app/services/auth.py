from datetime import timedelta
from sqlalchemy import select, update
from app.core.errors import AppError
from app.core.security import decode_token, hash_password, make_token, verify_password
from app.models import CUSTOMER, RefreshToken, User
from app.schemas import TokenOut
from app.services.common import audit


def issue_tokens(db, user):
    access, _, _ = make_token(user.id, "access")
    refresh, jti, exp = make_token(user.id, "refresh")
    db.add(RefreshToken(user_id=user.id, jti=jti, expires_at=exp))
    return TokenOut(access_token=access, refresh_token=refresh)


def create_user(db, data, role, actor_id=None):
    email = data.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise AppError("EMAIL_EXISTS", "A user with this email already exists", 409)
    u = User(email=email, password_hash=hash_password(data.password), full_name=data.full_name, role=role)
    db.add(u)
    db.flush()
    audit(db, actor_id or u.id, "USER_CREATED", "user", u.id, {"role": role})
    return u


def login(db, data):
    user = db.scalar(select(User).where(User.email == data.email.lower()))
    if not user or not verify_password(data.password, user.password_hash):
        audit(db, user.id if user else None, "LOGIN_FAILED", "user", user.id if user else None, {"email": data.email})
        db.commit()
        raise AppError("INVALID_CREDENTIALS", "Invalid email or password", 401)
    if not user.is_active:
        audit(db, user.id, "LOGIN_FAILED", "user", user.id, {"reason": "inactive"})
        db.commit()
        raise AppError("ACCOUNT_INACTIVE", "This account is deactivated", 403)
    tokens = issue_tokens(db, user)
    audit(db, user.id, "LOGIN", "user", user.id)
    db.commit()
    return tokens


def revoke_all(db, user_id):
    db.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
               .values(revoked=True))


def refresh(db, token):
    p = decode_token(token, "refresh")
    rt = db.scalar(select(RefreshToken).where(RefreshToken.jti == p["jti"]).with_for_update())
    if not rt:
        raise AppError("INVALID_TOKEN", "Unknown refresh token", 401)
    if rt.revoked:  # reuse of a rotated token: assume theft, kill the whole family
        revoke_all(db, rt.user_id)
        db.commit()
        raise AppError("TOKEN_REVOKED", "Refresh token has been revoked", 401)
    user = db.get(User, rt.user_id)
    if not user or not user.is_active:
        raise AppError("ACCOUNT_INACTIVE", "This account is deactivated", 403)
    rt.revoked = True
    tokens = issue_tokens(db, user)
    db.commit()
    return tokens


def logout(db, token):
    p = decode_token(token, "refresh")
    db.execute(update(RefreshToken).where(RefreshToken.jti == p["jti"]).values(revoked=True))
    db.commit()


def change_password(db, user, old, new):
    if not verify_password(old, user.password_hash):
        raise AppError("INVALID_CREDENTIALS", "Current password is incorrect", 401)
    user.password_hash = hash_password(new)
    revoke_all(db, user.id)
    audit(db, user.id, "PASSWORD_CHANGED", "user", user.id)
    db.commit()
