from fastapi import APIRouter, Depends
from sqlalchemy import select
from app.core.errors import AppError
from app.db import get_db
from app.deps import current_user, pager, require_roles
from app.models import ADMIN, CUSTOMER, MANAGER, Category, Product, User, Warehouse, WarehouseUser
from app.schemas import (ActiveIn, AdminUserIn, Page, ProductIn, ProductOut, ProductUpdate, UserOut, WarehouseIn,
                         WarehouseOut, WarehouseUpdate, WarehouseUserIn)
from app.services import auth as auth_svc
from app.services.common import assert_warehouse_access, audit, paginate, user_warehouse_ids

router = APIRouter(tags=["catalog"])


def _category(db, name):
    if not name:
        return None
    c = db.scalar(select(Category).where(Category.name == name))
    if not c:
        c = Category(name=name)
        db.add(c)
        db.flush()
    return c


def _product(db, pid):
    p = db.get(Product, pid)
    if not p:
        raise AppError("NOT_FOUND", "Product not found", 404)
    return p


# ---------------- products
@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(data: ProductIn, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    if db.scalar(select(Product.id).where(Product.sku == data.sku)):
        raise AppError("PRODUCT_SKU_EXISTS", f"SKU {data.sku} already exists", 409)
    p = Product(sku=data.sku, name=data.name, description=data.description, price=data.price,
                cost_price=data.cost_price, category_obj=_category(db, data.category))
    db.add(p)
    db.flush()
    audit(db, user.id, "PRODUCT_CREATED", "product", p.id, {"sku": p.sku})
    db.commit()
    return p


@router.get("/products", response_model=Page[ProductOut])
def list_products(q: str | None = None, category: str | None = None, is_active: bool | None = None,
                  pg=Depends(pager), db=Depends(get_db), user=Depends(current_user)):
    s = select(Product).order_by(Product.id)
    if user.role != ADMIN:
        is_active = True  # non-admins only see sellable products
    if is_active is not None:
        s = s.where(Product.is_active == is_active)
    if q:
        s = s.where(Product.name.ilike(f"%{q}%") | Product.sku.ilike(f"%{q}%"))
    if category:
        s = s.join(Category).where(Category.name == category)
    return paginate(db, s, *pg)


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db=Depends(get_db), user=Depends(current_user)):
    p = _product(db, product_id)
    if not p.is_active and user.role != ADMIN:
        raise AppError("NOT_FOUND", "Product not found", 404)
    return p


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, data: ProductUpdate, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    p = _product(db, product_id)
    changes = data.model_dump(exclude_unset=True)
    if "category" in changes:
        p.category_obj = _category(db, changes.pop("category"))
    for k, v in changes.items():
        setattr(p, k, v)
    audit(db, user.id, "PRODUCT_UPDATED", "product", p.id, {"fields": list(data.model_dump(exclude_unset=True))})
    db.commit()
    return p


def _set_product_active(db, user, pid, active):
    p = _product(db, pid)
    p.is_active = active
    audit(db, user.id, "PRODUCT_UPDATED", "product", p.id, {"is_active": active})
    db.commit()
    return p


@router.post("/products/{product_id}/activate", response_model=ProductOut)
def activate_product(product_id: int, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    return _set_product_active(db, user, product_id, True)


@router.post("/products/{product_id}/deactivate", response_model=ProductOut)
def deactivate_product(product_id: int, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    return _set_product_active(db, user, product_id, False)


# ---------------- warehouses
def _wh(db, wid):
    w = db.get(Warehouse, wid)
    if not w:
        raise AppError("NOT_FOUND", "Warehouse not found", 404)
    return w


@router.post("/warehouses", response_model=WarehouseOut, status_code=201)
def create_warehouse(data: WarehouseIn, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    if db.scalar(select(Warehouse.id).where(Warehouse.name == data.name)):
        raise AppError("WAREHOUSE_EXISTS", "Warehouse name already exists", 409)
    w = Warehouse(**data.model_dump())
    db.add(w)
    db.flush()
    audit(db, user.id, "WAREHOUSE_CREATED", "warehouse", w.id)
    db.commit()
    return w


@router.get("/warehouses", response_model=Page[WarehouseOut])
def list_warehouses(pg=Depends(pager), db=Depends(get_db), user=Depends(current_user)):
    s = select(Warehouse).order_by(Warehouse.id)
    if user.role == CUSTOMER:
        s = s.where(Warehouse.is_active.is_(True))
    else:
        ids = user_warehouse_ids(db, user)
        if ids is not None:
            s = s.where(Warehouse.id.in_(ids))
    return paginate(db, s, *pg)


@router.get("/warehouses/{warehouse_id}", response_model=WarehouseOut)
def get_warehouse(warehouse_id: int, db=Depends(get_db), user=Depends(current_user)):
    w = _wh(db, warehouse_id)
    if user.role == CUSTOMER:
        if not w.is_active:
            raise AppError("NOT_FOUND", "Warehouse not found", 404)
    else:
        assert_warehouse_access(db, user, w.id)
    return w


@router.patch("/warehouses/{warehouse_id}", response_model=WarehouseOut)
def update_warehouse(warehouse_id: int, data: WarehouseUpdate, db=Depends(get_db),
                     user=Depends(require_roles(ADMIN, MANAGER))):
    w = _wh(db, warehouse_id)
    assert_warehouse_access(db, user, w.id)
    for k, v in data.model_dump(exclude_unset=True).items():
        setattr(w, k, v)
    audit(db, user.id, "WAREHOUSE_UPDATED", "warehouse", w.id)
    db.commit()
    return w


def _set_wh_active(db, user, wid, active):
    w = _wh(db, wid)
    w.is_active = active
    audit(db, user.id, "WAREHOUSE_UPDATED", "warehouse", w.id, {"is_active": active})
    db.commit()
    return w


@router.post("/warehouses/{warehouse_id}/activate", response_model=WarehouseOut)
def activate_warehouse(warehouse_id: int, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    return _set_wh_active(db, user, warehouse_id, True)


@router.post("/warehouses/{warehouse_id}/deactivate", response_model=WarehouseOut)
def deactivate_warehouse(warehouse_id: int, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    return _set_wh_active(db, user, warehouse_id, False)


@router.post("/warehouses/{warehouse_id}/users", status_code=201)
def grant_warehouse_access(warehouse_id: int, data: WarehouseUserIn, db=Depends(get_db),
                           user=Depends(require_roles(ADMIN))):
    _wh(db, warehouse_id)
    target = db.get(User, data.user_id)
    if not target or target.role not in ("WAREHOUSE_MANAGER", "FULFILLMENT_AGENT"):
        raise AppError("INVALID_ASSIGNEE", "User must be a warehouse manager or fulfillment agent", 400)
    if not db.scalar(select(WarehouseUser.id).where(WarehouseUser.user_id == target.id,
                                                    WarehouseUser.warehouse_id == warehouse_id)):
        db.add(WarehouseUser(user_id=target.id, warehouse_id=warehouse_id))
        audit(db, user.id, "WAREHOUSE_ACCESS_GRANTED", "warehouse", warehouse_id, {"user_id": target.id})
    db.commit()
    return {"success": True}


# ---------------- admin: users
@router.post("/admin/users", response_model=UserOut, status_code=201)
def admin_create_user(data: AdminUserIn, db=Depends(get_db), user=Depends(require_roles(ADMIN))):
    u = auth_svc.create_user(db, data, data.role, actor_id=user.id)
    for wid in data.warehouse_ids:
        _wh(db, wid)
        db.add(WarehouseUser(user_id=u.id, warehouse_id=wid))
    db.commit()
    return u


@router.patch("/admin/users/{user_id}/active", response_model=UserOut)
def set_user_active(user_id: int, data: ActiveIn, db=Depends(get_db), admin=Depends(require_roles(ADMIN))):
    u = db.get(User, user_id)
    if not u:
        raise AppError("NOT_FOUND", "User not found", 404)
    u.is_active = data.is_active
    if not data.is_active:
        auth_svc.revoke_all(db, u.id)
    audit(db, admin.id, "USER_UPDATED", "user", u.id, {"is_active": data.is_active})
    db.commit()
    return u
