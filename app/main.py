from fastapi import FastAPI, Request
from app.core.errors import register_handlers
from app.routers import auth, catalog, inventory, misc, orders
from app.services.common import ip_var

app = FastAPI(title="Order Fulfillment & Inventory Management Platform", version="1.0.0")
register_handlers(app)


@app.middleware("http")
async def capture_ip(request: Request, call_next):
    ip_var.set(request.client.host if request.client else None)
    return await call_next(request)


for r in (auth.router, catalog.router, inventory.router, orders.router, misc.router):
    app.include_router(r, prefix="/api")


@app.get("/health", tags=["meta"])
def health():
    return {"status": "ok"}
