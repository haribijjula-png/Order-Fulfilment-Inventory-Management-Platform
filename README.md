# Order Fulfillment & Inventory Management Platform

FastAPI · PostgreSQL · SQLAlchemy 2 · Alembic · Pydantic · Pytest. JWT auth, RBAC + warehouse-level authorization,
locked inventory reservation, order/return state machines, idempotency, notifications, audit logs, dashboards.

## Quick start
```bash
# Docker (API + Postgres, runs migrations and seeds an admin)
docker compose up --build            # http://localhost:8000/docs

# Local
cp .env.example .env                 # edit DATABASE_URL / SECRET_KEY
pip install -r requirements.txt
alembic upgrade head
python -m app.seed                   # creates ADMIN_EMAIL / ADMIN_PASSWORD
uvicorn app.main:app --reload
```
Docs: Swagger `/docs`, ReDoc `/redoc`, plus `docs/openapi.json` and `docs/postman_collection.json`
(regenerate with `python scripts/export_docs.py`; the login request stores `access_token` automatically).

## Tests
```bash
pytest                                                    # SQLite, no setup needed
TEST_DATABASE_URL=postgresql+psycopg2://postgres:postgres@localhost:5432/fulfillment_test pytest
```
The concurrent-reservation test (8 parallel orders, 1 unit in stock) only runs on PostgreSQL because it depends on
row-level locks. SQLite ignores `FOR UPDATE`.

## Architecture
```mermaid
flowchart TD
  R[Routers app/routers] --> D[Dependencies: auth, roles, pagination]
  D --> S[Services: business rules, state machines]
  S --> Q[Repositories: query builders]
  S --> M[SQLAlchemy models]
  Q --> M --> P[(PostgreSQL)]
  S -. BackgroundTasks .-> N[Notification dispatch]
```
Routes only validate input and commit; rules live in `app/services/*`. Services `flush` but do not commit, so the
route (or `run_idempotent`) owns the transaction boundary and everything in one request commits or rolls back together.

## Roles and authorization
| Role | Capabilities |
|---|---|
| ADMIN | everything; creates staff (`POST /api/admin/users`), products, warehouses |
| WAREHOUSE_MANAGER | inventory adjust/transfer, order assignment, return handling in assigned warehouses |
| FULFILLMENT_AGENT | view/advance orders (accept → deliver) in assigned warehouses |
| CUSTOMER | public signup; create/list/cancel own orders, request returns |

Warehouse access is enforced by `assert_warehouse_access` (membership in `warehouse_users`); customers get 404 on
other people's orders. Inactive users cannot log in, and existing tokens stop working immediately.

## Concurrency strategy
1. **Reservation**: `SELECT ... FOR UPDATE` on the `(product, warehouse)` inventory row, then check and update inside one
   transaction. A second order for the same stock waits for the first to commit and then sees the reduced quantity.
   `CHECK (available_quantity >= 0)` is a database-level backstop, so negative inventory is impossible even with a bug.
2. **Deadlock avoidance**: locks are always taken in a fixed order: order items sorted by `product_id`; transfers lock
   both warehouses and inventory rows sorted by id.
3. **Status changes**: the order row is locked (`FOR UPDATE`) before validating the transition, so two concurrent
   cancel/ship calls cannot both succeed or double-release stock.
4. **Row creation races**: inventory rows are created inside a SAVEPOINT; on unique-violation we re-select and lock the winner's row.
5. **Idempotency**: `idempotency_keys` has a unique `(user, key, endpoint)`. The key row is inserted in the *same*
   transaction as the business work; a concurrent duplicate blocks on the unique index until the first commits, then
   replays the stored response. Same key with a different payload returns `409 DUPLICATE_IDEMPOTENCY_KEY`.
   Supported on `POST /orders`, `/orders/{id}/confirm-payment`, `/inventory/adjust`, `/inventory/transfer` via the
   `Idempotency-Key` header.
6. **Refresh tokens**: rotated on every use; reusing a rotated token revokes the whole token family.
7. Isolation level is PostgreSQL's default READ COMMITTED; correctness comes from explicit row locks plus constraints,
   not from SERIALIZABLE retries.

## Inventory model
`available_quantity` is sellable stock. A reservation moves units `available → reserved` (matching the assignment's
example 100/20 → 90/30); shipping consumes reserved (`SALE`); cancel/fail moves `reserved → available` (`RELEASE`);
a received return adds to available (`RETURN`). Every change writes an `inventory_transactions` row (signed quantity
for `ADJUSTMENT`, positive otherwise, with previous/new quantity, reason, user, reference, metadata).
`inventory_transactions` and `audit_logs` are append-only: no update/delete endpoints exist, and the migration
installs PostgreSQL triggers that reject `UPDATE`/`DELETE`.

## Order & return state machines
```
PENDING → CONFIRMED → PROCESSING → PACKED → SHIPPED → DELIVERED → RETURNED
   ↘ CANCELLED (from PENDING/CONFIRMED by customer; also PROCESSING/PACKED by staff)      PENDING → FAILED
```
Return: `REQUESTED → APPROVED|REJECTED`, `APPROVED → RECEIVED` (restocks), `RECEIVED → REFUNDED` (order → RETURNED).
Every order status change is stored in `order_status_history` and audited.

## Database design
```mermaid
erDiagram
  users ||--o{ warehouse_users : has
  warehouses ||--o{ warehouse_users : has
  categories ||--o{ products : groups
  products ||--o{ inventory : stocked_as
  warehouses ||--o{ inventory : holds
  inventory_transactions }o--|| products : of
  inventory_transactions }o--|| warehouses : at
  users ||--o{ orders : places
  warehouses ||--o{ orders : fulfils
  orders ||--|{ order_items : contains
  products ||--o{ order_items : snapshot_of
  orders ||--o{ order_status_history : logs
  orders ||--o{ order_assignments : assigned
  users ||--o{ order_assignments : agent
  orders ||--o{ returns : may_have
  returns ||--|{ return_items : contains
  users ||--o{ notifications : receives
  users ||--o{ audit_logs : performs
  users ||--o{ refresh_tokens : owns
  users ||--o{ idempotency_keys : owns
```
Constraints: unique SKU/email/order_number/`(product,warehouse)`/`(user,key,endpoint)`; `CHECK` on quantities ≥ 0,
prices ≥ 0, capacity > 0, role values. Indexes on foreign keys, order status/payment status/created_at.
`order_items.unit_price` is a snapshot, so later product price changes never alter existing orders.

## Errors
All errors use `{"success": false, "error": {"code", "message", "details?"}}` with 400/401/403/404/409/422/500.
Domain codes include `INSUFFICIENT_INVENTORY`, `INVALID_ORDER_STATUS`, `INVALID_RETURN_STATUS`,
`WAREHOUSE_ACCESS_DENIED`, `PRODUCT_INACTIVE`, `DUPLICATE_IDEMPOTENCY_KEY`, `INVALID_INVENTORY_TRANSFER`.

## Key endpoints
`/api/auth/*` · `/api/products` · `/api/warehouses` · `/api/inventory` (`/low-stock`, `/transactions`, `/adjust`,
`/transfer`) · `/api/orders` (+ `/cancel`, `/accept|process|pack|ship|deliver`, `/status`, `/assign`,
`/confirm-payment`, `/history`) · `/api/fulfillment/pending-orders` · `/api/returns` (+ `/approve|reject|receive|refund`)
· `/api/notifications` · `/api/dashboard` (role-aware) · `/api/audit-logs` (admin, read-only).
List endpoints take `page`, `page_size` (max 100), filters and `sort_by`/`order`.

## Assumptions and limitations
- Roles are a CHECK-constrained column rather than a separate `roles` table.
- The initial Alembic migration builds the schema from the models (`create_all`) and adds the append-only triggers;
  use `alembic revision --autogenerate` for future changes.
- Notifications are stored in the same transaction as the event (so they are never lost); `BackgroundTasks` runs a
  dispatch hook that currently logs. Swap in Celery/email/WebSockets there.
- Payment is modelled as a status plus an idempotent `confirm-payment` endpoint; there is no payment gateway.
- Revenue on the admin dashboard = total of delivered orders. Tax is a flat `TAX_RATE` (default 10%).
