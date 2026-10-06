from datetime import datetime, timezone
from sqlalchemy import (JSON, Boolean, CheckConstraint as CK, DateTime, ForeignKey as FK, Index,
                        Integer, Numeric, String, Text, UniqueConstraint as UQ)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column as col, relationship

ADMIN, MANAGER, AGENT, CUSTOMER = "ADMIN", "WAREHOUSE_MANAGER", "FULFILLMENT_AGENT", "CUSTOMER"
ROLES = (ADMIN, MANAGER, AGENT, CUSTOMER)
Money = Numeric(12, 2)


def utcnow():
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Stamped:
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class User(Base, Stamped):
    __tablename__ = "users"
    id: Mapped[int] = col(primary_key=True)
    email: Mapped[str] = col(String(255), unique=True, index=True)
    password_hash: Mapped[str] = col(String(255))
    full_name: Mapped[str] = col(String(120))
    role: Mapped[str] = col(String(30), index=True)  # replaces a separate roles table; validated by CHECK
    is_active: Mapped[bool] = col(Boolean, default=True)
    __table_args__ = (CK(role.in_(ROLES), name="ck_users_role"),)


class Category(Base):
    __tablename__ = "categories"
    id: Mapped[int] = col(primary_key=True)
    name: Mapped[str] = col(String(100), unique=True)


class Product(Base, Stamped):
    __tablename__ = "products"
    id: Mapped[int] = col(primary_key=True)
    sku: Mapped[str] = col(String(64), unique=True, index=True)
    name: Mapped[str] = col(String(200))
    description: Mapped[str | None] = col(Text)
    category_id: Mapped[int | None] = col(FK("categories.id"))
    price: Mapped[float] = col(Money)
    cost_price: Mapped[float] = col(Money)
    is_active: Mapped[bool] = col(Boolean, default=True)
    category_obj: Mapped[Category | None] = relationship(lazy="joined")
    __table_args__ = (CK("price >= 0", name="ck_product_price"), CK("cost_price >= 0", name="ck_product_cost"))

    @property
    def category(self):
        return self.category_obj.name if self.category_obj else None


class Warehouse(Base, Stamped):
    __tablename__ = "warehouses"
    id: Mapped[int] = col(primary_key=True)
    name: Mapped[str] = col(String(120), unique=True)
    location: Mapped[str] = col(String(255))
    capacity: Mapped[int] = col(Integer)
    is_active: Mapped[bool] = col(Boolean, default=True)
    __table_args__ = (CK("capacity > 0", name="ck_wh_capacity"),)


class WarehouseUser(Base):
    __tablename__ = "warehouse_users"
    id: Mapped[int] = col(primary_key=True)
    user_id: Mapped[int] = col(FK("users.id"), index=True)
    warehouse_id: Mapped[int] = col(FK("warehouses.id"), index=True)
    __table_args__ = (UQ("user_id", "warehouse_id"),)


class Inventory(Base, Stamped):
    __tablename__ = "inventory"
    id: Mapped[int] = col(primary_key=True)
    product_id: Mapped[int] = col(FK("products.id"))
    warehouse_id: Mapped[int] = col(FK("warehouses.id"), index=True)
    available_quantity: Mapped[int] = col(Integer, default=0)
    reserved_quantity: Mapped[int] = col(Integer, default=0)
    damaged_quantity: Mapped[int] = col(Integer, default=0)
    reorder_level: Mapped[int] = col(Integer, default=10)
    __table_args__ = (UQ("product_id", "warehouse_id"),
                      CK("available_quantity >= 0", name="ck_inv_available"),
                      CK("reserved_quantity >= 0", name="ck_inv_reserved"),
                      CK("damaged_quantity >= 0", name="ck_inv_damaged"))


class InventoryTransaction(Base):  # append-only (DB trigger on PostgreSQL)
    __tablename__ = "inventory_transactions"
    id: Mapped[int] = col(primary_key=True)
    product_id: Mapped[int] = col(FK("products.id"), index=True)
    warehouse_id: Mapped[int] = col(FK("warehouses.id"), index=True)
    type: Mapped[str] = col(String(20))
    quantity: Mapped[int] = col(Integer)
    previous_quantity: Mapped[int | None] = col(Integer)
    new_quantity: Mapped[int | None] = col(Integer)
    reason: Mapped[str | None] = col(String(255))
    reference_entity: Mapped[str | None] = col(String(50))
    reference_id: Mapped[int | None] = col(Integer)
    user_id: Mapped[int | None] = col(FK("users.id"))
    meta: Mapped[dict | None] = col("metadata", JSON)
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index("ix_invtx_ref", "reference_entity", "reference_id"),)


class Order(Base, Stamped):
    __tablename__ = "orders"
    id: Mapped[int] = col(primary_key=True)
    order_number: Mapped[str] = col(String(40), unique=True)
    customer_id: Mapped[int] = col(FK("users.id"), index=True)
    warehouse_id: Mapped[int] = col(FK("warehouses.id"), index=True)
    total_amount: Mapped[float] = col(Money, default=0)
    status: Mapped[str] = col(String(20), index=True, default="PENDING")
    payment_status: Mapped[str] = col(String(20), index=True, default="PENDING")
    expected_delivery_date: Mapped[datetime | None] = col(DateTime(timezone=True))
    items: Mapped[list["OrderItem"]] = relationship(lazy="selectin", order_by="OrderItem.id")
    __table_args__ = (CK("total_amount >= 0", name="ck_order_total"), Index("ix_orders_created", "created_at"))


class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = col(primary_key=True)
    order_id: Mapped[int] = col(FK("orders.id"), index=True)
    product_id: Mapped[int] = col(FK("products.id"))
    quantity: Mapped[int] = col(Integer)
    unit_price: Mapped[float] = col(Money)  # snapshot at order time
    discount: Mapped[float] = col(Money, default=0)
    tax: Mapped[float] = col(Money, default=0)
    total: Mapped[float] = col(Money)
    __table_args__ = (CK("quantity > 0", name="ck_item_qty"),)


class OrderStatusHistory(Base):
    __tablename__ = "order_status_history"
    id: Mapped[int] = col(primary_key=True)
    order_id: Mapped[int] = col(FK("orders.id"), index=True)
    from_status: Mapped[str | None] = col(String(20))
    to_status: Mapped[str] = col(String(20))
    changed_by: Mapped[int | None] = col(FK("users.id"))
    note: Mapped[str | None] = col(String(255))
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)


class OrderAssignment(Base):
    __tablename__ = "order_assignments"
    id: Mapped[int] = col(primary_key=True)
    order_id: Mapped[int] = col(FK("orders.id"), index=True)
    agent_id: Mapped[int] = col(FK("users.id"), index=True)
    assigned_by: Mapped[int | None] = col(FK("users.id"))
    is_active: Mapped[bool] = col(Boolean, default=True)
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)


class ReturnRequest(Base, Stamped):
    __tablename__ = "returns"
    id: Mapped[int] = col(primary_key=True)
    order_id: Mapped[int] = col(FK("orders.id"), index=True)
    customer_id: Mapped[int] = col(FK("users.id"), index=True)
    status: Mapped[str] = col(String(20), default="REQUESTED", index=True)
    reason: Mapped[str] = col(String(500))
    items: Mapped[list["ReturnItem"]] = relationship(lazy="selectin")


class ReturnItem(Base):
    __tablename__ = "return_items"
    id: Mapped[int] = col(primary_key=True)
    return_id: Mapped[int] = col(FK("returns.id"), index=True)
    product_id: Mapped[int] = col(FK("products.id"))
    quantity: Mapped[int] = col(Integer)
    __table_args__ = (CK("quantity > 0", name="ck_retitem_qty"),)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = col(primary_key=True)
    user_id: Mapped[int] = col(FK("users.id"), index=True)
    type: Mapped[str] = col(String(40))
    message: Mapped[str] = col(String(500))
    is_read: Mapped[bool] = col(Boolean, default=False)
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)


class AuditLog(Base):  # append-only (DB trigger on PostgreSQL)
    __tablename__ = "audit_logs"
    id: Mapped[int] = col(primary_key=True)
    user_id: Mapped[int | None] = col(FK("users.id"), index=True)
    action: Mapped[str] = col(String(50), index=True)
    entity: Mapped[str] = col(String(50))
    entity_id: Mapped[str | None] = col(String(50))
    ip_address: Mapped[str | None] = col(String(64))
    meta: Mapped[dict | None] = col("metadata", JSON)
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"
    id: Mapped[int] = col(primary_key=True)
    user_id: Mapped[int] = col(FK("users.id"), index=True)
    jti: Mapped[str] = col(String(64), unique=True)
    expires_at: Mapped[datetime] = col(DateTime(timezone=True))
    revoked: Mapped[bool] = col(Boolean, default=False)
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    id: Mapped[int] = col(primary_key=True)
    user_id: Mapped[int] = col(FK("users.id"))
    key: Mapped[str] = col(String(120))
    endpoint: Mapped[str] = col(String(120))
    request_hash: Mapped[str] = col(String(64))
    response_body: Mapped[dict | None] = col(JSON)
    status_code: Mapped[int | None] = col(Integer)
    created_at: Mapped[datetime] = col(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UQ("user_id", "key", "endpoint", name="uq_idem"),)
