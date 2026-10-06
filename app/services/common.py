import logging
from contextvars import ContextVar
from sqlalchemy import func, select
from app.core.errors import AppError
from app.models import ADMIN, AGENT, MANAGER, AuditLog, Notification, User, WarehouseUser

ip_var: ContextVar = ContextVar("ip", default=None)
log = logging.getLogger("app.notify")


def audit(db, user_id, action, entity, entity_id=None, meta=None):
    db.add(AuditLog(user_id=user_id, action=action, entity=entity,
                    entity_id=None if entity_id is None else str(entity_id),
                    ip_address=ip_var.get(), meta=meta or {}))


def notify(db, user_ids, type_, message):
    for uid in set(user_ids):
        db.add(Notification(user_id=uid, type=type_, message=message))


def dispatch(event: str, ref: str):
    """BackgroundTasks hook: stand-in for email/push delivery (swap for Celery in production)."""
    log.info("notification dispatched: %s %s", event, ref)


def staff_ids(db, warehouse_id):
    admins = db.scalars(select(User.id).where(User.role == ADMIN, User.is_active.is_(True))).all()
    mgrs = db.scalars(select(WarehouseUser.user_id).join(User, User.id == WarehouseUser.user_id)
                      .where(WarehouseUser.warehouse_id == warehouse_id, User.role == MANAGER,
                             User.is_active.is_(True))).all()
    return list(admins) + list(mgrs)


def user_warehouse_ids(db, user):
    """None means unrestricted (admin)."""
    if user.role == ADMIN:
        return None
    return list(db.scalars(select(WarehouseUser.warehouse_id).where(WarehouseUser.user_id == user.id)).all())


def assert_warehouse_access(db, user, warehouse_id):
    if user.role == ADMIN:
        return
    ok = user.role in (MANAGER, AGENT) and db.scalar(
        select(WarehouseUser.id).where(WarehouseUser.user_id == user.id,
                                       WarehouseUser.warehouse_id == warehouse_id)) is not None
    if not ok:
        raise AppError("WAREHOUSE_ACCESS_DENIED", "You do not have access to this warehouse", 403)


def paginate(db, stmt, page, page_size):
    total = db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = db.scalars(stmt.limit(page_size).offset((page - 1) * page_size)).unique().all()
    return {"items": rows, "total": total, "page": page, "page_size": page_size}
