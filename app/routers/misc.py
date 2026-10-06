from datetime import timezone
from fastapi import APIRouter, Depends
from sqlalchemy import func, select, update
from app.core.errors import AppError
from app.db import get_db
from app.deps import current_user, pager, require_roles
from app.models import ADMIN, CUSTOMER, AuditLog, Inventory, Notification, Order, OrderStatusHistory, utcnow
from app.schemas import AuditOut, NotificationOut, Page
from app.services.common import paginate, user_warehouse_ids

router = APIRouter(tags=["misc"])
TERMINAL = ("DELIVERED", "CANCELLED", "FAILED", "RETURNED")


@router.get("/notifications", response_model=Page[NotificationOut])
def list_notifications(unread_only: bool = False, pg=Depends(pager), db=Depends(get_db), user=Depends(current_user)):
    s = select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc())
    if unread_only:
        s = s.where(Notification.is_read.is_(False))
    return paginate(db, s, *pg)


@router.patch("/notifications/read-all")
def read_all(db=Depends(get_db), user=Depends(current_user)):
    res = db.execute(update(Notification).where(Notification.user_id == user.id, Notification.is_read.is_(False))
                     .values(is_read=True))
    db.commit()
    return {"updated": res.rowcount}


@router.patch("/notifications/{notification_id}/read", response_model=NotificationOut)
def read_one(notification_id: int, db=Depends(get_db), user=Depends(current_user)):
    n = db.get(Notification, notification_id)
    if not n or n.user_id != user.id:
        raise AppError("NOT_FOUND", "Notification not found", 404)
    n.is_read = True
    db.commit()
    return n


@router.get("/audit-logs", response_model=Page[AuditOut])
def audit_logs(action: str | None = None, user_id: int | None = None, pg=Depends(pager), db=Depends(get_db),
               user=Depends(require_roles(ADMIN))):
    s = select(AuditLog).order_by(AuditLog.id.desc())  # read-only: no write endpoints exist
    if action:
        s = s.where(AuditLog.action == action)
    if user_id:
        s = s.where(AuditLog.user_id == user_id)
    return paginate(db, s, *pg)


def _count(db, *where):
    return db.scalar(select(func.count()).select_from(Order).where(*where)) or 0


@router.get("/dashboard")
def dashboard(db=Depends(get_db), user=Depends(current_user)):
    if user.role == CUSTOMER:
        mine = Order.customer_id == user.id
        spent = db.scalar(select(func.coalesce(func.sum(Order.total_amount), 0))
                          .where(mine, Order.status.notin_(("CANCELLED", "FAILED", "RETURNED"))))
        return {"total_orders": _count(db, mine), "active_orders": _count(db, mine, Order.status.notin_(TERMINAL)),
                "completed_orders": _count(db, mine, Order.status == "DELIVERED"),
                "cancelled_orders": _count(db, mine, Order.status == "CANCELLED"), "total_spending": spent}
    ids = user_warehouse_ids(db, user)
    scope_o = [Order.warehouse_id.in_(ids)] if ids is not None else []
    scope_i = [Inventory.warehouse_id.in_(ids)] if ids is not None else []
    low = db.scalar(select(func.count()).select_from(Inventory)
                    .where(*scope_i, Inventory.available_quantity <= Inventory.reorder_level))
    if user.role == ADMIN:
        out = db.scalar(select(func.count()).select_from(Inventory).where(Inventory.available_quantity == 0))
        revenue = db.scalar(select(func.coalesce(func.sum(Order.total_amount), 0)).where(Order.status == "DELIVERED"))
        return {"total_orders": _count(db), "pending_orders": _count(db, Order.status == "PENDING"),
                "processing_orders": _count(db, Order.status == "PROCESSING"),
                "completed_orders": _count(db, Order.status == "DELIVERED"),
                "cancelled_orders": _count(db, Order.status == "CANCELLED"), "total_revenue": revenue,
                "low_stock_products": low, "out_of_stock_products": out}
    start = utcnow().astimezone(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    shipped = db.scalar(select(func.count()).select_from(OrderStatusHistory)
                        .join(Order, Order.id == OrderStatusHistory.order_id)
                        .where(*scope_o, OrderStatusHistory.to_status == "SHIPPED",
                               OrderStatusHistory.created_at >= start))
    stock = db.execute(select(Inventory.warehouse_id, func.sum(Inventory.available_quantity),
                              func.sum(Inventory.reserved_quantity)).where(*scope_i)
                       .group_by(Inventory.warehouse_id)).all()
    return {"warehouse_inventory": [{"warehouse_id": w, "available": int(a or 0), "reserved": int(r or 0)}
                                    for w, a, r in stock],
            "pending_fulfillment": _count(db, *scope_o, Order.status.in_(("PENDING", "CONFIRMED"))),
            "processing_orders": _count(db, *scope_o, Order.status == "PROCESSING"),
            "low_stock_items": low, "todays_shipments": shipped}
