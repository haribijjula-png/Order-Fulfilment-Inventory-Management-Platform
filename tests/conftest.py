import os
TEST_URL = os.environ.get("TEST_DATABASE_URL", "sqlite:///./test_suite.db")
os.environ["DATABASE_URL"] = TEST_URL
os.environ["BCRYPT_ROUNDS"] = "4"

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from app.core.security import hash_password
from app.db import get_db
from app.main import app
from app.models import ADMIN, AGENT, CUSTOMER, MANAGER, Base, User, Warehouse, WarehouseUser

PW = "Passw0rd!"
IS_PG = TEST_URL.startswith("postgresql")


@pytest.fixture()
def Session():
    kw = {"connect_args": {"check_same_thread": False}} if not IS_PG else {}
    engine = create_engine(TEST_URL, **kw)
    if not IS_PG:  # make SAVEPOINTs work on pysqlite
        @event.listens_for(engine, "connect")
        def _c(dbapi, _):
            dbapi.isolation_level = None

        @event.listens_for(engine, "begin")
        def _b(conn):
            conn.exec_driver_sql("BEGIN")
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    S = sessionmaker(bind=engine, expire_on_commit=False)

    def override():
        db = S()
        try:
            yield db
        finally:
            db.close()
    app.dependency_overrides[get_db] = override
    yield S
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture()
def client(Session):
    return TestClient(app)


def mk_user(Session, role, email, wh=(), active=True):
    with Session() as db:
        u = User(email=email, password_hash=hash_password(PW), full_name=email.split("@")[0], role=role,
                 is_active=active)
        db.add(u)
        db.flush()
        for w in wh:
            db.add(WarehouseUser(user_id=u.id, warehouse_id=w))
        db.commit()
        return u.id


def login(client, email, pw=PW):
    r = client.post("/api/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def hdr(client, email):
    return {"Authorization": f"Bearer {login(client, email)['access_token']}"}


@pytest.fixture()
def world(client, Session):
    """admin, 2 warehouses, product P1 ($10), manager/agent on wh1, customer."""
    with Session() as db:
        w1, w2 = Warehouse(name="W1", location="A", capacity=10000), Warehouse(name="W2", location="B", capacity=10000)
        db.add_all([w1, w2])
        db.commit()
        wh1, wh2 = w1.id, w2.id
    ids = {"admin": mk_user(Session, ADMIN, "admin@t.io"),
           "manager": mk_user(Session, MANAGER, "mgr@t.io", [wh1]),
           "agent": mk_user(Session, AGENT, "agent@t.io", [wh1]),
           "agent2": mk_user(Session, AGENT, "agent2@t.io", [wh2]),
           "customer": mk_user(Session, CUSTOMER, "cust@t.io"),
           "customer2": mk_user(Session, CUSTOMER, "cust2@t.io")}
    h = {k: hdr(client, f"{e}@t.io") for k, e in
         {"admin": "admin", "manager": "mgr", "agent": "agent", "customer": "cust", "customer2": "cust2"}.items()}
    r = client.post("/api/products", headers=h["admin"],
                    json={"sku": "SKU-1001", "name": "Widget", "price": "10.00", "cost_price": "4.00", "category": "Tools"})
    assert r.status_code == 201, r.text
    return {"h": h, "ids": ids, "wh1": wh1, "wh2": wh2, "pid": r.json()["id"], "Session": Session}


def stock(client, w, qty, wh=None, pid=None, key=None):
    r = client.post("/api/inventory/adjust", headers=w["h"]["admin"],
                    json={"product_id": pid or w["pid"], "warehouse_id": wh or w["wh1"], "quantity": qty,
                          "reason": "seed", "type": "PURCHASE"})
    assert r.status_code == 200, r.text
    return r.json()


def place(client, w, qty=1, discount="0", key=None, who="customer", pid=None):
    hh = dict(w["h"][who])
    if key:
        hh["Idempotency-Key"] = key
    return client.post("/api/orders", headers=hh, json={"warehouse_id": w["wh1"],
                       "items": [{"product_id": pid or w["pid"], "quantity": qty, "discount": discount}]})


def inv(client, w, wh=None):
    r = client.get("/api/inventory", headers=w["h"]["admin"], params={"warehouse_id": wh or w["wh1"]})
    return r.json()["items"][0]
