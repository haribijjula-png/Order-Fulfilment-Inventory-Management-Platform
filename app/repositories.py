from sqlalchemy import asc, desc, select
from app.models import ADMIN, CUSTOMER, Inventory, Order
from app.services.common import user_warehouse_ids

ORDER_SORT = {"created_at": Order.created_at, "total_amount": Order.total_amount, "status": Order.status, "id": Order.id}


def orders_stmt(db, user, status=None, customer_id=None, warehouse_id=None, payment_status=None,
                date_from=None, date_to=None, min_total=None, max_total=None, sort_by="created_at", order="desc"):
    s = select(Order)
    if user.role == CUSTOMER:
        s = s.where(Order.customer_id == user.id)
    else:
        ids = user_warehouse_ids(db, user)
        if ids is not None:
            s = s.where(Order.warehouse_id.in_(ids))
        if customer_id:
            s = s.where(Order.customer_id == customer_id)
    for col, val in ((Order.status, status and status.upper()), (Order.warehouse_id, warehouse_id),
                     (Order.payment_status, payment_status and payment_status.upper())):
        if val:
            s = s.where(col == val)
    if date_from:
        s = s.where(Order.created_at >= date_from)
    if date_to:
        s = s.where(Order.created_at <= date_to)
    if min_total is not None:
        s = s.where(Order.total_amount >= min_total)
    if max_total is not None:
        s = s.where(Order.total_amount <= max_total)
    col = ORDER_SORT.get(sort_by, Order.created_at)
    return s.order_by(asc(col) if order == "asc" else desc(col), Order.id.desc())


def inventory_stmt(db, user, warehouse_id=None, product_id=None, low_stock=None, out_of_stock=None):
    s = select(Inventory)
    ids = user_warehouse_ids(db, user)
    if ids is not None:
        s = s.where(Inventory.warehouse_id.in_(ids))
    if warehouse_id:
        s = s.where(Inventory.warehouse_id == warehouse_id)
    if product_id:
        s = s.where(Inventory.product_id == product_id)
    if low_stock:
        s = s.where(Inventory.available_quantity <= Inventory.reorder_level)
    if out_of_stock:
        s = s.where(Inventory.available_quantity == 0)
    return s.order_by(Inventory.warehouse_id, Inventory.product_id)
