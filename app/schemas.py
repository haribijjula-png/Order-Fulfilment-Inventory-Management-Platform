from datetime import datetime
from decimal import Decimal
from typing import Generic, Literal, TypeVar
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

T = TypeVar("T")


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    page: int
    page_size: int


# ---- auth / users
class RegisterIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    full_name: str = Field(min_length=1, max_length=120)


class LoginIn(BaseModel):
    email: EmailStr
    password: str


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    old_password: str
    new_password: str = Field(min_length=8, max_length=128)


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserOut(ORM):
    id: int
    email: str
    full_name: str
    role: str
    is_active: bool


class AdminUserIn(RegisterIn):
    role: Literal["ADMIN", "WAREHOUSE_MANAGER", "FULFILLMENT_AGENT", "CUSTOMER"]
    warehouse_ids: list[int] = []


class ActiveIn(BaseModel):
    is_active: bool


class WarehouseUserIn(BaseModel):
    user_id: int


# ---- catalog
class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    category: str | None = None
    price: Decimal = Field(ge=0, decimal_places=2)
    cost_price: Decimal = Field(ge=0, decimal_places=2)


class ProductUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    category: str | None = None
    price: Decimal | None = Field(None, ge=0, decimal_places=2)
    cost_price: Decimal | None = Field(None, ge=0, decimal_places=2)


class ProductOut(ORM):
    id: int
    sku: str
    name: str
    description: str | None
    category: str | None
    price: Decimal
    cost_price: Decimal
    is_active: bool
    created_at: datetime
    updated_at: datetime


class WarehouseIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    location: str = Field(min_length=1, max_length=255)
    capacity: int = Field(gt=0)


class WarehouseUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=120)
    location: str | None = None
    capacity: int | None = Field(None, gt=0)


class WarehouseOut(ORM):
    id: int
    name: str
    location: str
    capacity: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ---- inventory
class InventoryOut(ORM):
    id: int
    product_id: int
    warehouse_id: int
    available_quantity: int
    reserved_quantity: int
    damaged_quantity: int
    reorder_level: int
    updated_at: datetime


class InventoryTxOut(ORM):
    id: int
    product_id: int
    warehouse_id: int
    type: str
    quantity: int
    previous_quantity: int | None
    new_quantity: int | None
    reason: str | None
    reference_entity: str | None
    reference_id: int | None
    user_id: int | None
    meta: dict | None
    created_at: datetime


class AdjustIn(BaseModel):
    product_id: int
    warehouse_id: int
    quantity: int
    reason: str = Field(min_length=1, max_length=255)
    type: Literal["ADJUSTMENT", "PURCHASE", "DAMAGE"] = "ADJUSTMENT"
    reorder_level: int | None = Field(None, ge=0)

    @model_validator(mode="after")
    def _check(self):
        if self.quantity == 0:
            raise ValueError("quantity must be non-zero")
        if self.type in ("PURCHASE", "DAMAGE") and self.quantity < 0:
            raise ValueError(f"{self.type} quantity must be positive")
        return self


class TransferIn(BaseModel):
    product_id: int
    from_warehouse_id: int
    to_warehouse_id: int
    quantity: int = Field(gt=0)
    reason: str | None = None


# ---- orders
class OrderItemIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)
    discount: Decimal = Field(Decimal("0"), ge=0)


class OrderIn(BaseModel):
    warehouse_id: int
    items: list[OrderItemIn] = Field(min_length=1)

    @field_validator("items")
    @classmethod
    def _unique(cls, v):
        if len({i.product_id for i in v}) != len(v):
            raise ValueError("duplicate product_id in items")
        return v


class OrderItemOut(ORM):
    product_id: int
    quantity: int
    unit_price: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal


class OrderOut(ORM):
    id: int
    order_number: str
    customer_id: int
    warehouse_id: int
    total_amount: Decimal
    status: str
    payment_status: str
    expected_delivery_date: datetime | None
    created_at: datetime
    updated_at: datetime
    items: list[OrderItemOut]


class StatusIn(BaseModel):
    status: Literal["CONFIRMED", "PROCESSING", "PACKED", "SHIPPED", "DELIVERED", "CANCELLED", "FAILED"]
    note: str | None = None


class AssignIn(BaseModel):
    agent_id: int


class HistoryOut(ORM):
    from_status: str | None
    to_status: str
    changed_by: int | None
    note: str | None
    created_at: datetime


class ReturnItemIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)


class ReturnIn(BaseModel):
    order_id: int
    reason: str = Field(min_length=1, max_length=500)
    items: list[ReturnItemIn] = Field(min_length=1)


class ReturnOut(ORM):
    id: int
    order_id: int
    customer_id: int
    status: str
    reason: str
    items: list[ReturnItemIn]
    created_at: datetime


class NotificationOut(ORM):
    id: int
    type: str
    message: str
    is_read: bool
    created_at: datetime


class AuditOut(ORM):
    id: int
    user_id: int | None
    action: str
    entity: str
    entity_id: str | None
    ip_address: str | None
    meta: dict | None
    created_at: datetime
