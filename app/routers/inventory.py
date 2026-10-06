from fastapi import APIRouter, Depends, Header
from sqlalchemy import select
from app.db import get_db
from app.deps import pager, require_roles
from app.models import ADMIN, AGENT, MANAGER, Inventory, InventoryTransaction
from app.repositories import inventory_stmt
from app.schemas import AdjustIn, InventoryOut, InventoryTxOut, Page, TransferIn
from app.services import inventory as svc
from app.services.common import assert_warehouse_access, paginate, user_warehouse_ids
from app.services.idempotency import run_idempotent

router = APIRouter(prefix="/inventory", tags=["inventory"])
STAFF = (ADMIN, MANAGER, AGENT)


@router.get("", response_model=Page[InventoryOut])
def list_inventory(warehouse_id: int | None = None, product_id: int | None = None, low_stock: bool | None = None,
                   out_of_stock: bool | None = None, pg=Depends(pager), db=Depends(get_db),
                   user=Depends(require_roles(*STAFF))):
    return paginate(db, inventory_stmt(db, user, warehouse_id, product_id, low_stock, out_of_stock), *pg)


@router.get("/low-stock", response_model=Page[InventoryOut])
def low_stock(warehouse_id: int | None = None, pg=Depends(pager), db=Depends(get_db),
              user=Depends(require_roles(*STAFF))):
    return paginate(db, inventory_stmt(db, user, warehouse_id, low_stock=True), *pg)


@router.get("/transactions", response_model=Page[InventoryTxOut])
def transactions(product_id: int | None = None, warehouse_id: int | None = None, pg=Depends(pager),
                 db=Depends(get_db), user=Depends(require_roles(ADMIN, MANAGER))):
    s = select(InventoryTransaction).order_by(InventoryTransaction.id.desc())
    ids = user_warehouse_ids(db, user)
    if ids is not None:
        s = s.where(InventoryTransaction.warehouse_id.in_(ids))
    if product_id:
        s = s.where(InventoryTransaction.product_id == product_id)
    if warehouse_id:
        s = s.where(InventoryTransaction.warehouse_id == warehouse_id)
    return paginate(db, s, *pg)


@router.post("/adjust", response_model=InventoryOut)
def adjust(data: AdjustIn, db=Depends(get_db), user=Depends(require_roles(ADMIN, MANAGER)),
           key: str | None = Header(None, alias="Idempotency-Key")):
    assert_warehouse_access(db, user, data.warehouse_id)
    return run_idempotent(db, user.id, key, "inventory.adjust", data.model_dump(),
                          lambda: InventoryOut.model_validate(svc.adjust(db, user, data)))


@router.post("/transfer")
def transfer(data: TransferIn, db=Depends(get_db), user=Depends(require_roles(ADMIN, MANAGER)),
             key: str | None = Header(None, alias="Idempotency-Key")):
    assert_warehouse_access(db, user, data.from_warehouse_id)
    assert_warehouse_access(db, user, data.to_warehouse_id)

    def work():
        r = svc.transfer(db, user, data)
        return {"from": InventoryOut.model_validate(r["from"]), "to": InventoryOut.model_validate(r["to"])}
    return run_idempotent(db, user.id, key, "inventory.transfer", data.model_dump(), work)


@router.get("/{product_id}", response_model=list[InventoryOut])
def product_inventory(product_id: int, db=Depends(get_db), user=Depends(require_roles(*STAFF))):
    svc.get_product(db, product_id)
    return db.scalars(inventory_stmt(db, user, product_id=product_id)).all()
