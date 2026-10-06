import hashlib
import json
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from app.core.errors import AppError
from app.models import IdempotencyKey


def _get(db, user_id, key, endpoint):
    return db.scalar(select(IdempotencyKey).where(IdempotencyKey.user_id == user_id,
                                                  IdempotencyKey.key == key,
                                                  IdempotencyKey.endpoint == endpoint))


def run_idempotent(db, user_id, key, endpoint, payload, fn):
    """Run fn() once per (user, key, endpoint). fn must not commit; this function commits.

    The idempotency row is inserted in the SAME transaction as the business work, guarded by a
    unique constraint. A concurrent duplicate blocks on that unique index until the first request
    commits, then replays the stored response.
    """
    if not key:
        result = jsonable_encoder(fn())
        db.commit()
        return result
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    row = _get(db, user_id, key, endpoint)
    created = False
    if row is None:
        try:
            with db.begin_nested():
                row = IdempotencyKey(user_id=user_id, key=key, endpoint=endpoint, request_hash=digest)
                db.add(row)
                db.flush()
            created = True
        except IntegrityError:
            row = _get(db, user_id, key, endpoint)
    if not created:
        if row is None or row.response_body is None:
            raise AppError("DUPLICATE_IDEMPOTENCY_KEY", "A request with this idempotency key is in progress", 409)
        if row.request_hash != digest:
            raise AppError("DUPLICATE_IDEMPOTENCY_KEY", "Idempotency key was already used with a different payload", 409)
        return row.response_body
    body = jsonable_encoder(fn())
    row.response_body, row.status_code = body, 200
    db.commit()
    return body
