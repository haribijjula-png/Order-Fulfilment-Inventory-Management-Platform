from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query
from sqlalchemy import select
from app.db import get_db
from app.deps import current_user, pager, require_roles
from app.models import ADMIN, AGENT, CUSTOMER, MANAGER, Order, OrderStatusHistory, ReturnRequest
from app.repositories import orders_stmt
from app.schemas import (AssignIn, HistoryOut, OrderIn, OrderOut, Page, ReturnIn, ReturnOut, StatusIn)
from app.services import orders as svc
from app.services.common import dispatch, paginate, user_warehouse_ids
from app.services.idempotency import run_idempotent

router = APIRouter(tags=["orders"])
STAFF = (ADMIN, MANAGER, AGENT)


@router.post("/orders", response_model=OrderOut, status_code=201)
def create_order(data: OrderIn, bg: BackgroundTasks, db=Depends(get_db), user=Depends(require_roles(CUSTOMER)),
                 key: str | None = Header(None, alias="Idempotency-Key")):
    out = run_idempotent(db, user.id, key, "orders.create", data.model_dump(mode="json"),
                         lambda: OrderOut.model_validate(svc.create_order(db, user, data)))
    bg.add_task(dispatch, "ORDER_CREATED", out["order_number"])
    return out


@router.get("/orders", response_model=Page[OrderOut])
def list_orders(status: str | None = None, customer_id: int | None = None, warehouse_id: int | None = None,
                payment_status: str | None = None, date_from: datetime | None = None,
                date_to: datetime | None = None, min_total: Decimal | None = None, max_total: Decimal | None = None,
                sort_by: str = "created_at", order: str = Query("desc", pattern="^(asc|desc)$"),
                pg=Depends(pager), db=Depends(get_db), user=Depends(current_user)):
    s = orders_stmt(db, user, status, customer_id, warehouse_id, payment_status, date_from, date_to,
                    min_total, max_total, sort_by, order)
    return paginate(db, s, *pg)


@router.get("/fulfillment/pending-orders", response_model=Page[OrderOut])
def pending_orders(pg=Depends(pager), db=Depends(get_db), user=Depends(require_roles(*STAFF))):
    return paginate(db, orders_stmt(db, user, status="PENDING", sort_by="created_at", order="asc"), *pg)


@router.get("/orders/{order_id}", response_model=OrderOut)
def get_order(order_id: int, db=Depends(get_db), user=Depends(current_user)):
    return svc.get_order(db, user, order_id)


@router.get("/orders/{order_id}/history", response_model=list[HistoryOut])
def order_history(order_id: int, db=Depends(get_db), user=Depends(current_user)):
    svc.get_order(db, user, order_id)
    return db.scalars(select(OrderStatusHistory).where(OrderStatusHistory.order_id == order_id)
                      .order_by(OrderStatusHistory.id)).all()


def _transition(db, bg, user, order_id, new, note=None):
    o = svc.transition(db, user, order_id, new, note)
    db.commit()
    bg.add_task(dispatch, f"ORDER_{new}", o.order_number)
    return o


@router.post("/orders/{order_id}/cancel", response_model=OrderOut)
def cancel(order_id: int, bg: BackgroundTasks, db=Depends(get_db),
           user=Depends(require_roles(CUSTOMER, ADMIN, MANAGER))):
    return _transition(db, bg, user, order_id, "CANCELLED")


@router.post("/orders/{order_id}/status", response_model=OrderOut)
def set_status(order_id: int, data: StatusIn, bg: BackgroundTasks, db=Depends(get_db),
               user=Depends(require_roles(*STAFF))):
    return _transition(db, bg, user, order_id, data.status, data.note)


def _action(name, target):
    @router.post(f"/orders/{{order_id}}/{name}", response_model=OrderOut, operation_id=f"order_{name}",
                 summary=f"{name.title()} order -> {target}")
    def act(order_id: int, bg: BackgroundTasks, db=Depends(get_db), user=Depends(require_roles(*STAFF))):
        return _transition(db, bg, user, order_id, target)


for _n, _t in {"accept": "CONFIRMED", "process": "PROCESSING", "pack": "PACKED", "ship": "SHIPPED",
               "deliver": "DELIVERED"}.items():
    _action(_n, _t)


@router.post("/orders/{order_id}/assign", response_model=OrderOut)
def assign(order_id: int, data: AssignIn, bg: BackgroundTasks, db=Depends(get_db),
           user=Depends(require_roles(ADMIN, MANAGER, CUSTOMER, AGENT))):
    o = svc.assign(db, user, order_id, data.agent_id)
    db.commit()
    bg.add_task(dispatch, "ORDER_ASSIGNED", o.order_number)
    return o


@router.post("/orders/{order_id}/confirm-payment", response_model=OrderOut)
def confirm_payment(order_id: int, db=Depends(get_db), user=Depends(require_roles(CUSTOMER, ADMIN, MANAGER)),
                    key: str | None = Header(None, alias="Idempotency-Key")):
    return run_idempotent(db, user.id, key, f"orders.pay.{order_id}", {"order_id": order_id},
                          lambda: OrderOut.model_validate(svc.confirm_payment(db, user, order_id)))


# ---------------- returns
@router.post("/returns", response_model=ReturnOut, status_code=201)
def create_return(data: ReturnIn, db=Depends(get_db), user=Depends(require_roles(CUSTOMER))):
    r = svc.create_return(db, user, data)
    db.commit()
    return r


@router.get("/returns", response_model=Page[ReturnOut])
def list_returns(status: str | None = None, pg=Depends(pager), db=Depends(get_db), user=Depends(current_user)):
    s = select(ReturnRequest).order_by(ReturnRequest.id.desc())
    if user.role == CUSTOMER:
        s = s.where(ReturnRequest.customer_id == user.id)
    else:
        ids = user_warehouse_ids(db, user)
        if ids is not None:
            s = s.join(Order, Order.id == ReturnRequest.order_id).where(Order.warehouse_id.in_(ids))
    if status:
        s = s.where(ReturnRequest.status == status.upper())
    return paginate(db, s, *pg)


def _return_action(name, target):
    @router.post(f"/returns/{{return_id}}/{name}", response_model=ReturnOut, operation_id=f"return_{name}")
    def act(return_id: int, db=Depends(get_db), user=Depends(require_roles(ADMIN, MANAGER))):
        r = svc.return_action(db, user, return_id, target)
        db.commit()
        return r


for _n, _t in {"approve": "APPROVED", "reject": "REJECTED", "receive": "RECEIVED", "refund": "REFUNDED"}.items():
    _return_action(_n, _t)
