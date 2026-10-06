from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from app.core.errors import AppError
from app.models import Inventory, InventoryTransaction, Product, Warehouse
from app.services.common import audit, notify, staff_ids


def get_product(db, pid):
    p = db.get(Product, pid)
    if not p:
        raise AppError("NOT_FOUND", "Product not found", 404)
    return p


def get_warehouse(db, wid, lock=False):
    q = select(Warehouse).where(Warehouse.id == wid)
    w = db.scalar(q.with_for_update() if lock else q)
    if not w:
        raise AppError("NOT_FOUND", "Warehouse not found", 404)
    return w


def lock_inventory(db, product_id, warehouse_id, create=False):
    """SELECT ... FOR UPDATE on the (product, warehouse) row; optionally create it race-safely."""
    q = select(Inventory).where(Inventory.product_id == product_id, Inventory.warehouse_id == warehouse_id)
    inv = db.scalar(q.with_for_update())
    if inv is None and create:
        try:
            with db.begin_nested():
                inv = Inventory(product_id=product_id, warehouse_id=warehouse_id, available_quantity=0,
                                reserved_quantity=0, damaged_quantity=0)
                db.add(inv)
                db.flush()
        except IntegrityError:  # someone else created it first: lock theirs
            inv = db.scalar(q.with_for_update())
    return inv


def record(db, product_id, warehouse_id, type_, qty, user_id, ref=None, ref_id=None,
           prev=None, new=None, reason=None, meta=None):
    db.add(InventoryTransaction(product_id=product_id, warehouse_id=warehouse_id, type=type_, quantity=qty,
                                user_id=user_id, reference_entity=ref, reference_id=ref_id,
                                previous_quantity=prev, new_quantity=new, reason=reason, meta=meta or {}))


def _low_stock(db, inv, product, prev):
    if prev > inv.reorder_level >= inv.available_quantity:  # fire only when crossing the threshold
        notify(db, staff_ids(db, inv.warehouse_id), "LOW_STOCK",
               f"Low stock for {product.sku}: {inv.available_quantity} left (reorder level {inv.reorder_level})")


def _check_capacity(db, wh, extra):
    used = db.scalar(select(func.coalesce(func.sum(Inventory.available_quantity + Inventory.reserved_quantity
                                                   + Inventory.damaged_quantity), 0))
                     .where(Inventory.warehouse_id == wh.id))
    if used + extra > wh.capacity:
        raise AppError("WAREHOUSE_CAPACITY_EXCEEDED", f"Warehouse {wh.name} capacity exceeded", 409)


def _insufficient(product):
    return AppError("INSUFFICIENT_INVENTORY", f"Insufficient inventory for product {product.sku}", 409)


def adjust(db, user, data):
    product = get_product(db, data.product_id)
    delta = -data.quantity if data.type == "DAMAGE" else data.quantity
    wh = get_warehouse(db, data.warehouse_id, lock=delta > 0)  # serialise stock-ins for capacity check
    if delta > 0:
        _check_capacity(db, wh, delta)
    inv = lock_inventory(db, product.id, wh.id, create=True)
    prev, new = inv.available_quantity, inv.available_quantity + delta
    if new < 0:
        raise _insufficient(product)
    inv.available_quantity = new
    if data.type == "DAMAGE":
        inv.damaged_quantity += data.quantity
    if data.reorder_level is not None:
        inv.reorder_level = data.reorder_level
    record(db, product.id, wh.id, data.type if data.type != "ADJUSTMENT" else "ADJUSTMENT", delta, user.id,
           prev=prev, new=new, reason=data.reason)
    audit(db, user.id, "INVENTORY_ADJUSTED", "inventory", inv.id,
          {"product_id": product.id, "warehouse_id": wh.id, "delta": delta, "reason": data.reason})
    _low_stock(db, inv, product, prev)
    db.flush()
    return inv


def transfer(db, user, data):
    if data.from_warehouse_id == data.to_warehouse_id:
        raise AppError("INVALID_INVENTORY_TRANSFER", "Source and destination warehouses must differ", 400)
    product = get_product(db, data.product_id)
    whs = {w: get_warehouse(db, w, lock=True) for w in sorted({data.from_warehouse_id, data.to_warehouse_id})}
    src_wh, dst_wh = whs[data.from_warehouse_id], whs[data.to_warehouse_id]
    if not (src_wh.is_active and dst_wh.is_active):
        raise AppError("INVALID_INVENTORY_TRANSFER", "Both warehouses must be active", 409)
    _check_capacity(db, dst_wh, data.quantity)
    invs = {}
    for wid in sorted(whs):  # consistent lock order prevents deadlocks
        invs[wid] = lock_inventory(db, product.id, wid, create=(wid == dst_wh.id))
    src, dst = invs[src_wh.id], invs[dst_wh.id]
    if src is None or src.available_quantity < data.quantity:
        raise _insufficient(product)
    sp, dp = src.available_quantity, dst.available_quantity
    src.available_quantity -= data.quantity
    dst.available_quantity += data.quantity
    meta = {"from": src_wh.id, "to": dst_wh.id}
    record(db, product.id, src_wh.id, "TRANSFER_OUT", data.quantity, user.id, prev=sp, new=src.available_quantity,
           reason=data.reason, meta=meta)
    record(db, product.id, dst_wh.id, "TRANSFER_IN", data.quantity, user.id, prev=dp, new=dst.available_quantity,
           reason=data.reason, meta=meta)
    audit(db, user.id, "INVENTORY_TRANSFERRED", "inventory", src.id, {**meta, "product_id": product.id, "qty": data.quantity})
    notify(db, staff_ids(db, src_wh.id) + staff_ids(db, dst_wh.id), "INVENTORY_TRANSFER",
           f"Transferred {data.quantity} x {product.sku} from {src_wh.name} to {dst_wh.name}")
    _low_stock(db, src, product, sp)
    db.flush()
    return {"from": src, "to": dst}


def reserve(db, product, warehouse_id, qty, order_id, user_id):
    inv = lock_inventory(db, product.id, warehouse_id)
    if inv is None or inv.available_quantity < qty:
        raise _insufficient(product)
    prev = inv.available_quantity
    inv.available_quantity -= qty
    inv.reserved_quantity += qty
    record(db, product.id, warehouse_id, "RESERVATION", qty, user_id, "order", order_id, prev, inv.available_quantity)
    audit(db, user_id, "INVENTORY_RESERVED", "inventory", inv.id, {"order_id": order_id, "qty": qty})
    _low_stock(db, inv, product, prev)


def release(db, product_id, warehouse_id, qty, order_id, user_id):
    inv = lock_inventory(db, product_id, warehouse_id)
    prev = inv.available_quantity
    inv.available_quantity += qty
    inv.reserved_quantity -= qty
    record(db, product_id, warehouse_id, "RELEASE", qty, user_id, "order", order_id, prev, inv.available_quantity)
    audit(db, user_id, "INVENTORY_RELEASED", "inventory", inv.id, {"order_id": order_id, "qty": qty})


def fulfill_sale(db, product_id, warehouse_id, qty, order_id, user_id):
    inv = lock_inventory(db, product_id, warehouse_id)
    inv.reserved_quantity -= qty
    record(db, product_id, warehouse_id, "SALE", qty, user_id, "order", order_id,
           inv.available_quantity, inv.available_quantity)


def restock_return(db, product_id, warehouse_id, qty, return_id, user_id):
    inv = lock_inventory(db, product_id, warehouse_id, create=True)
    prev = inv.available_quantity
    inv.available_quantity += qty
    record(db, product_id, warehouse_id, "RETURN", qty, user_id, "return", return_id, prev, inv.available_quantity)
