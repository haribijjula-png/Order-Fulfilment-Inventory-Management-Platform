import threading
from decimal import Decimal
import pytest
from tests.conftest import IS_PG, inv, mk_user, place, stock
from app.models import AGENT


def D(x):
    return Decimal(str(x))


def adv(client, w, oid, action, who="manager"):
    return client.post(f"/api/orders/{oid}/{action}", headers=w["h"][who])


def deliver(client, w, oid):
    for a in ("accept", "process", "pack", "ship", "deliver"):
        assert adv(client, w, oid, a).status_code == 200


def test_order_calculation_and_reservation(client, world):
    stock(client, world, 100)
    r = place(client, world, qty=3, discount="5.00")  # gross 30, net 25, tax 10% = 2.50
    assert r.status_code == 201, r.text
    o = r.json()
    it = o["items"][0]
    assert D(it["tax"]) == D("2.50") and D(it["total"]) == D("27.50") and D(o["total_amount"]) == D("27.50")
    assert o["status"] == "PENDING" and o["payment_status"] == "PENDING"
    i = inv(client, world)
    assert (i["available_quantity"], i["reserved_quantity"]) == (97, 3)


def test_item_price_snapshot_survives_price_change(client, world):
    stock(client, world, 10)
    oid = place(client, world).json()["id"]
    client.patch(f"/api/products/{world['pid']}", headers=world["h"]["admin"], json={"price": "99.00"})
    assert D(client.get(f"/api/orders/{oid}", headers=world["h"]["customer"]).json()["items"][0]["unit_price"]) == D("10.00")


def test_insufficient_inventory_and_inactive_product(client, world):
    stock(client, world, 2)
    r = place(client, world, qty=3)
    assert r.status_code == 409 and r.json()["error"] == {"code": "INSUFFICIENT_INVENTORY", "message": "Insufficient inventory for product SKU-1001"}
    assert client.get("/api/orders", headers=world["h"]["customer"]).json()["total"] == 0
    assert inv(client, world)["available_quantity"] == 2
    types = [n["type"] for n in client.get("/api/notifications", headers=world["h"]["customer"]).json()["items"]]
    assert "ORDER_PROCESSING_FAILED" in types
    client.post(f"/api/products/{world['pid']}/deactivate", headers=world["h"]["admin"])
    r = place(client, world)
    assert r.status_code == 409 and r.json()["error"]["code"] == "PRODUCT_INACTIVE"


def test_state_machine_full_flow_and_invalid_transitions(client, world):
    stock(client, world, 10)
    oid = place(client, world, qty=2).json()["id"]
    r = adv(client, world, oid, "pack")  # PENDING -> PACKED not allowed
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_ORDER_STATUS"
    deliver(client, world, oid)
    r = client.post(f"/api/orders/{oid}/status", headers=world["h"]["manager"], json={"status": "PROCESSING"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_ORDER_STATUS"  # Delivered -> Processing
    hist = [h["to_status"] for h in client.get(f"/api/orders/{oid}/history", headers=world["h"]["customer"]).json()]
    assert hist == ["PENDING", "CONFIRMED", "PROCESSING", "PACKED", "SHIPPED", "DELIVERED"]
    i = inv(client, world)  # shipped: reserved consumed, available unchanged
    assert (i["available_quantity"], i["reserved_quantity"]) == (8, 0)
    types = {n["type"] for n in client.get("/api/notifications", headers=world["h"]["customer"]).json()["items"]}
    assert {"ORDER_CREATED", "ORDER_CONFIRMED", "ORDER_SHIPPED", "ORDER_DELIVERED"} <= types


def test_cancellation_releases_inventory_atomically(client, world):
    stock(client, world, 10)
    oid = place(client, world, qty=4).json()["id"]
    r = client.post(f"/api/orders/{oid}/cancel", headers=world["h"]["customer"])
    assert r.status_code == 200 and r.json()["status"] == "CANCELLED"
    i = inv(client, world)
    assert (i["available_quantity"], i["reserved_quantity"]) == (10, 0)
    types = {t["type"] for t in client.get("/api/inventory/transactions", headers=world["h"]["admin"]).json()["items"]}
    assert "RELEASE" in types
    assert "ORDER_CANCELLED" in {n["type"] for n in client.get("/api/notifications", headers=world["h"]["customer"]).json()["items"]}
    assert "ORDER_CANCELLED" in {a["action"] for a in client.get("/api/audit-logs", headers=world["h"]["admin"]).json()["items"]}
    assert client.post(f"/api/orders/{oid}/cancel", headers=world["h"]["customer"]).status_code == 409


def test_customer_cancel_restrictions_and_isolation(client, world):
    stock(client, world, 10)
    oid = place(client, world).json()["id"]
    assert client.get(f"/api/orders/{oid}", headers=world["h"]["customer2"]).status_code == 404
    assert client.post(f"/api/orders/{oid}/cancel", headers=world["h"]["customer2"]).status_code == 404
    adv(client, world, oid, "accept"); adv(client, world, oid, "process")
    r = client.post(f"/api/orders/{oid}/cancel", headers=world["h"]["customer"])
    assert r.status_code == 409 and r.json()["error"]["code"] == "INVALID_ORDER_STATUS"
    assert client.post(f"/api/orders/{oid}/accept", headers=world["h"]["customer"]).status_code == 403


def test_idempotent_order_creation(client, world):
    stock(client, world, 10)
    a, b = place(client, world, qty=2, key="k1"), place(client, world, qty=2, key="k1")
    assert a.status_code == 201 and b.json()["id"] == a.json()["id"]
    assert client.get("/api/orders", headers=world["h"]["customer"]).json()["total"] == 1
    assert inv(client, world)["available_quantity"] == 8
    c = place(client, world, qty=3, key="k1")
    assert c.status_code == 409 and c.json()["error"]["code"] == "DUPLICATE_IDEMPOTENCY_KEY"


def test_assignment_rules(client, world):
    stock(client, world, 10)
    oid = place(client, world).json()["id"]
    ids = world["ids"]
    body = lambda a: {"agent_id": a}
    assert client.post(f"/api/orders/{oid}/assign", headers=world["h"]["customer"], json=body(ids["agent"])).status_code == 403
    r = client.post(f"/api/orders/{oid}/assign", headers=world["h"]["manager"], json=body(ids["agent2"]))  # agent of wh2
    assert r.status_code == 403 and r.json()["error"]["code"] == "WAREHOUSE_ACCESS_DENIED"
    off = mk_user(world["Session"], AGENT, "off@t.io", [world["wh1"]], active=False)
    r = client.post(f"/api/orders/{oid}/assign", headers=world["h"]["manager"], json=body(off))
    assert r.status_code == 409 and r.json()["error"]["code"] == "AGENT_INACTIVE"
    assert client.post(f"/api/orders/{oid}/assign", headers=world["h"]["manager"], json=body(ids["agent"])).status_code == 200
    agent2 = mk_user(world["Session"], AGENT, "agent3@t.io", [world["wh1"]])
    assert client.post(f"/api/orders/{oid}/assign", headers=world["h"]["manager"], json=body(agent2)).status_code == 200
    assert "ORDER_ASSIGNED" in {a["action"] for a in client.get("/api/audit-logs", headers=world["h"]["admin"]).json()["items"]}
    assert any(n["type"] == "ORDER_ASSIGNED" for n in client.get("/api/notifications", headers=world["h"]["agent"]).json()["items"])


def test_agent_scope_and_pending_view(client, world):
    stock(client, world, 10)
    oid = place(client, world).json()["id"]
    assert client.get("/api/fulfillment/pending-orders", headers=world["h"]["agent"]).json()["total"] == 1
    assert adv(client, world, oid, "accept", who="agent").status_code == 200
    assert client.get("/api/orders", headers=world["h"]["agent"], params={"status": "confirmed"}).json()["total"] == 1


def test_order_filters_pagination_and_dashboards(client, world):
    stock(client, world, 100)
    for q in (1, 2, 3):
        place(client, world, qty=q)
    r = client.get("/api/orders", headers=world["h"]["customer"], params={"page": 1, "page_size": 2, "sort_by": "total_amount", "order": "asc"})
    j = r.json()
    assert j["total"] == 3 and len(j["items"]) == 2 and D(j["items"][0]["total_amount"]) == D("11.00")
    assert client.get("/api/orders", headers=world["h"]["admin"], params={"min_total": "20"}).json()["total"] == 2
    assert client.get("/api/orders", headers=world["h"]["admin"], params={"page_size": 1000}).status_code == 422
    adm = client.get("/api/dashboard", headers=world["h"]["admin"]).json()
    assert adm["total_orders"] == 3 and adm["pending_orders"] == 3
    assert client.get("/api/dashboard", headers=world["h"]["customer"]).json()["active_orders"] == 3
    assert client.get("/api/dashboard", headers=world["h"]["manager"]).json()["pending_fulfillment"] == 3


def test_notifications_read(client, world):
    stock(client, world, 5)
    place(client, world)
    n = client.get("/api/notifications", headers=world["h"]["customer"]).json()["items"][0]
    assert client.patch(f"/api/notifications/{n['id']}/read", headers=world["h"]["customer"]).json()["is_read"] is True
    assert client.patch(f"/api/notifications/{n['id']}/read", headers=world["h"]["customer2"]).status_code == 404
    assert client.patch("/api/notifications/read-all", headers=world["h"]["customer"]).status_code == 200


# ---------------- returns
def test_return_flow_restocks_inventory(client, world):
    stock(client, world, 10)
    oid = place(client, world, qty=2).json()["id"]
    cust = world["h"]["customer"]
    body = {"order_id": oid, "reason": "damaged", "items": [{"product_id": world["pid"], "quantity": 2}]}
    assert client.post("/api/returns", headers=cust, json=body).status_code == 409  # not delivered yet
    deliver(client, world, oid)
    rid = client.post("/api/returns", headers=cust, json=body).json()["id"]
    assert client.post("/api/returns", headers=cust, json=body).status_code == 409  # duplicate
    m = world["h"]["manager"]
    assert client.post(f"/api/returns/{rid}/receive", headers=m).json()["error"]["code"] == "INVALID_RETURN_STATUS"
    for a, s in (("approve", "APPROVED"), ("receive", "RECEIVED"), ("refund", "REFUNDED")):
        assert client.post(f"/api/returns/{rid}/{a}", headers=m).json()["status"] == s
    assert inv(client, world)["available_quantity"] == 10  # 8 + 2 returned
    assert client.get(f"/api/orders/{oid}", headers=cust).json()["status"] == "RETURNED"
    assert client.post(f"/api/returns/{rid}/approve", headers=m).status_code == 409


def test_return_rejection_and_validation(client, world):
    stock(client, world, 10)
    oid = place(client, world, qty=2).json()["id"]
    deliver(client, world, oid)
    cust = world["h"]["customer"]
    too_many = {"order_id": oid, "reason": "x", "items": [{"product_id": world["pid"], "quantity": 5}]}
    assert client.post("/api/returns", headers=cust, json=too_many).status_code == 400
    assert client.post("/api/returns", headers=world["h"]["customer2"], json={**too_many, "items": [{"product_id": world["pid"], "quantity": 1}]}).status_code == 404
    rid = client.post("/api/returns", headers=cust, json={**too_many, "items": [{"product_id": world["pid"], "quantity": 1}]}).json()["id"]
    assert client.post(f"/api/returns/{rid}/reject", headers=world["h"]["manager"]).json()["status"] == "REJECTED"
    assert client.post(f"/api/returns/{rid}/approve", headers=world["h"]["manager"]).status_code == 409
    assert client.post(f"/api/returns/{rid}/approve", headers=cust).status_code == 403


@pytest.mark.skipif(not IS_PG, reason="row-level locking needs PostgreSQL (set TEST_DATABASE_URL)")
def test_concurrent_reservation_never_oversells(client, world):
    stock(client, world, 1)
    results = []

    def go(i):
        results.append(place(client, world, who="customer" if i % 2 else "customer2").status_code)
    ts = [threading.Thread(target=go, args=(i,)) for i in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert sorted(results) == [201] + [409] * 7
    i = inv(client, world)
    assert (i["available_quantity"], i["reserved_quantity"]) == (0, 1)
