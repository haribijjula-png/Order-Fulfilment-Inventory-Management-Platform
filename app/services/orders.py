import uuid
from datetime import timedelta
from decimal import ROUND_HALF_UP, Decimal
from sqlalchemy import select
from app.core.config import settings
from app.core.errors import AppError
from app.models import (ADMIN, AGENT, CUSTOMER, MANAGER, Order, OrderAssignment, OrderItem, OrderStatusHistory,
                        Product, ReturnItem, ReturnRequest, User, WarehouseUser, utcnow)
from app.services import inventory as inv
from app.services.common import assert_warehouse_access, audit, notify, staff_ids

TRANSITIONS = {
    "PENDING": {"CONFIRMED", "CANCELLED", "FAILED"},
    "CONFIRMED": {"PROCESSING", "CANCELLED"},
    "PROCESSING": {"PACKED", "CANCELLED"},
    "PACKED": {"SHIPPED", "CANCELLED"},
    "SHIPPED": {"DELIVERED"},
    "DELIVERED": {"RETURNED"},
    "CANCELLED": set(), "FAILED": set(), "RETURNED": set(),
}
CUSTOMER_CANCELLABLE = {"PENDING", "CONFIRMED"}
AGENT_ALLOWED = {"CONFIRMED", "PROCESSING", "PACKED", "SHIPPED", "DELIVERED"}
NOTIFY = {"CONFIRMED": "ORDER_CONFIRMED", "SHIPPED": "ORDER_SHIPPED", "DELIVERED": "ORDER_DELIVERED",
          "CANCELLED": "ORDER_CANCELLED"}
RETURN_TRANSITIONS = {"REQUESTED": {"APPROVED", "REJECTED"}, "APPROVED": {"RECEIVED"},
                      "RECEIVED": {"REFUNDED"}, "REJECTED": set(), "REFUNDED": set()}
CENT = Decimal("0.01")


def _lock_order(db, order_id):
    o = db.scalar(select(Order).where(Order.id == order_id).with_for_update())
    if not o:
        raise AppError("NOT_FOUND", "Order not found", 404)
    return o


def assert_order_access(db, user, order):
    if user.role == CUSTOMER:
        if order.customer_id != user.id:
            raise AppError("NOT_FOUND", "Order not found", 404)
    else:
        assert_warehouse_access(db, user, order.warehouse_id)


def get_order(db, user, order_id):
    o = db.get(Order, order_id)
    if not o:
        raise AppError("NOT_FOUND", "Order not found", 404)
    assert_order_access(db, user, o)
    return o


def create_order(db, user, data):
    uid = user.id
    try:
        return _create(db, uid, data)
    except AppError as e:
        if e.code in ("INSUFFICIENT_INVENTORY", "PRODUCT_INACTIVE"):
            db.rollback()  # undo partial reservations, then durably record the failure
            notify(db, [uid], "ORDER_PROCESSING_FAILED", f"Order could not be processed: {e.message}")
            db.commit()
        raise


def _create(db, uid, data):
    wh = inv.get_warehouse(db, data.warehouse_id)
    if not wh.is_active:
        raise AppError("WAREHOUSE_INACTIVE", "Warehouse is not active", 409)
    order = Order(order_number=f"ORD-{utcnow():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}", customer_id=uid,
                  warehouse_id=wh.id, status="PENDING", payment_status="PENDING",
                  expected_delivery_date=utcnow() + timedelta(days=5), total_amount=0)
    db.add(order)
    db.flush()
    total = Decimal("0")
    for it in sorted(data.items, key=lambda i: i.product_id):  # ordered locking => no deadlocks
        p = db.get(Product, it.product_id)
        if not p:
            raise AppError("NOT_FOUND", f"Product {it.product_id} not found", 404)
        if not p.is_active:
            raise AppError("PRODUCT_INACTIVE", f"Product {p.sku} is inactive", 409)
        gross = Decimal(p.price) * it.quantity
        if it.discount > gross:
            raise AppError("INVALID_DISCOUNT", f"Discount exceeds line amount for {p.sku}", 400)
        tax = ((gross - it.discount) * Decimal(str(settings.tax_rate))).quantize(CENT, ROUND_HALF_UP)
        line = gross - it.discount + tax
        inv.reserve(db, p, wh.id, it.quantity, order.id, uid)
        db.add(OrderItem(order_id=order.id, product_id=p.id, quantity=it.quantity, unit_price=p.price,
                         discount=it.discount, tax=tax, total=line))
        total += line
    order.total_amount = total
    db.add(OrderStatusHistory(order_id=order.id, from_status=None, to_status="PENDING", changed_by=uid))
    audit(db, uid, "ORDER_CREATED", "order", order.id, {"total": str(total)})
    notify(db, [uid], "ORDER_CREATED", f"Order {order.order_number} was created")
    notify(db, staff_ids(db, wh.id), "ORDER_CREATED", f"New order {order.order_number} awaiting fulfillment")
    db.flush()
    db.refresh(order)
    return order


def apply_status(db, order, new, user_id, note=None):
    if new not in TRANSITIONS.get(order.status, set()):
        raise AppError("INVALID_ORDER_STATUS", f"Cannot change order from {order.status} to {new}", 409)
    old, order.status = order.status, new
    items = sorted(order.items, key=lambda i: i.product_id)
    if new in ("CANCELLED", "FAILED"):
        for i in items:
            inv.release(db, i.product_id, order.warehouse_id, i.quantity, order.id, user_id)
        if order.payment_status == "PAID":
            order.payment_status = "REFUNDED"
    elif new == "SHIPPED":
        for i in items:
            inv.fulfill_sale(db, i.product_id, order.warehouse_id, i.quantity, order.id, user_id)
    db.add(OrderStatusHistory(order_id=order.id, from_status=old, to_status=new, changed_by=user_id, note=note))
    audit(db, user_id, "ORDER_CANCELLED" if new == "CANCELLED" else "ORDER_STATUS_CHANGED", "order", order.id,
          {"from": old, "to": new})
    if new in NOTIFY:
        notify(db, [order.customer_id], NOTIFY[new], f"Order {order.order_number} is now {new}")
    if new == "FAILED":
        notify(db, [order.customer_id] + staff_ids(db, order.warehouse_id), "ORDER_PROCESSING_FAILED",
               f"Order {order.order_number} failed")


def active_assignment(db, order_id):
    return db.scalar(select(OrderAssignment).where(OrderAssignment.order_id == order_id,
                                                   OrderAssignment.is_active.is_(True)))


def transition(db, user, order_id, new, note=None):
    order = _lock_order(db, order_id)  # row lock serialises concurrent status changes
    assert_order_access(db, user, order)
    if user.role == CUSTOMER:
        if new != "CANCELLED":
            raise AppError("FORBIDDEN", "Customers may only cancel orders", 403)
        if order.status not in CUSTOMER_CANCELLABLE:
            raise AppError("INVALID_ORDER_STATUS", f"Order can no longer be cancelled by the customer ({order.status})", 409)
    elif user.role == AGENT and new not in AGENT_ALLOWED:
        raise AppError("FORBIDDEN", "Fulfillment agents cannot perform this transition", 403)
    apply_status(db, order, new, user.id, note)
    if new == "CONFIRMED" and user.role == AGENT and not active_assignment(db, order.id):
        db.add(OrderAssignment(order_id=order.id, agent_id=user.id, assigned_by=user.id))
    db.flush()
    return order


def assign(db, user, order_id, agent_id):
    if user.role == CUSTOMER:
        raise AppError("FORBIDDEN", "Customers cannot assign orders", 403)
    order = _lock_order(db, order_id)
    assert_warehouse_access(db, user, order.warehouse_id)
    if order.status in ("DELIVERED", "CANCELLED", "FAILED", "RETURNED"):
        raise AppError("INVALID_ORDER_STATUS", f"Cannot assign an order in status {order.status}", 409)
    agent = db.get(User, agent_id)
    if not agent or agent.role != AGENT:
        raise AppError("INVALID_ASSIGNEE", "Assignee must be a fulfillment agent", 400)
    if not agent.is_active:
        raise AppError("AGENT_INACTIVE", "Inactive agents cannot receive orders", 409)
    if not db.scalar(select(WarehouseUser.id).where(WarehouseUser.user_id == agent.id,
                                                    WarehouseUser.warehouse_id == order.warehouse_id)):
        raise AppError("WAREHOUSE_ACCESS_DENIED", "Agent is not authorized for this order's warehouse", 403)
    prev = active_assignment(db, order.id)
    if prev and prev.agent_id == agent.id:
        return order
    if prev:
        prev.is_active = False
        notify(db, [prev.agent_id], "ORDER_ASSIGNED", f"Order {order.order_number} was reassigned away from you")
    db.add(OrderAssignment(order_id=order.id, agent_id=agent.id, assigned_by=user.id))
    audit(db, user.id, "ORDER_ASSIGNED", "order", order.id,
          {"agent_id": agent.id, "previous_agent_id": prev.agent_id if prev else None})
    notify(db, [agent.id], "ORDER_ASSIGNED", f"Order {order.order_number} was assigned to you")
    db.flush()
    return order


def confirm_payment(db, user, order_id):
    order = _lock_order(db, order_id)
    assert_order_access(db, user, order)
    if order.status in ("CANCELLED", "FAILED"):
        raise AppError("INVALID_ORDER_STATUS", "Cannot pay for a cancelled or failed order", 409)
    if order.payment_status != "PAID":
        order.payment_status = "PAID"
        audit(db, user.id, "PAYMENT_CONFIRMED", "order", order.id)
    db.flush()
    return order


# ---------------- returns
def create_return(db, user, data):
    order = _lock_order(db, data.order_id)
    if order.customer_id != user.id:
        raise AppError("NOT_FOUND", "Order not found", 404)
    if order.status != "DELIVERED":
        raise AppError("RETURN_NOT_ELIGIBLE", "Only delivered orders can be returned", 409)
    if db.scalar(select(ReturnRequest.id).where(ReturnRequest.order_id == order.id,
                                                ReturnRequest.status != "REJECTED")):
        raise AppError("RETURN_NOT_ELIGIBLE", "A return already exists for this order", 409)
    ordered = {i.product_id: i.quantity for i in order.items}
    for it in data.items:
        if it.quantity > ordered.get(it.product_id, 0):
            raise AppError("RETURN_NOT_ELIGIBLE", f"Invalid return quantity for product {it.product_id}", 400)
    r = ReturnRequest(order_id=order.id, customer_id=user.id, reason=data.reason, status="REQUESTED")
    r.items = [ReturnItem(product_id=i.product_id, quantity=i.quantity) for i in data.items]
    db.add(r)
    db.flush()
    audit(db, user.id, "RETURN_CREATED", "return", r.id, {"order_id": order.id})
    notify(db, staff_ids(db, order.warehouse_id), "RETURN_REQUESTED", f"Return requested for {order.order_number}")
    return r


def return_action(db, user, return_id, new):
    r = db.scalar(select(ReturnRequest).where(ReturnRequest.id == return_id).with_for_update())
    if not r:
        raise AppError("NOT_FOUND", "Return not found", 404)
    order = _lock_order(db, r.order_id)
    assert_warehouse_access(db, user, order.warehouse_id)
    if new not in RETURN_TRANSITIONS[r.status]:
        raise AppError("INVALID_RETURN_STATUS", f"Cannot change return from {r.status} to {new}", 409)
    r.status = new
    if new == "RECEIVED":
        for i in sorted(r.items, key=lambda x: x.product_id):
            inv.restock_return(db, i.product_id, order.warehouse_id, i.quantity, r.id, user.id)
    elif new == "REFUNDED":
        order.payment_status = "REFUNDED"
        apply_status(db, order, "RETURNED", user.id, "return refunded")
    if new == "APPROVED":
        audit(db, user.id, "RETURN_APPROVED", "return", r.id)
        notify(db, [r.customer_id], "RETURN_APPROVED", f"Your return for {order.order_number} was approved")
    else:
        audit(db, user.id, f"RETURN_{new}", "return", r.id)
        notify(db, [r.customer_id], f"RETURN_{new}", f"Your return for {order.order_number} is {new}")
    db.flush()
    return r
