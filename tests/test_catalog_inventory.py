from tests.conftest import inv, stock


def test_role_and_warehouse_authorization(client, world):
    w, h = world, world["h"]
    assert client.post("/api/products", headers=h["customer"], json={"sku": "X", "name": "x", "price": "1", "cost_price": "1"}).status_code == 403
    assert client.post("/api/products", json={}).status_code == 401
    body = {"product_id": w["pid"], "warehouse_id": w["wh2"], "quantity": 5, "reason": "x", "type": "PURCHASE"}
    r = client.post("/api/inventory/adjust", headers=h["manager"], json=body)  # manager only owns wh1
    assert r.status_code == 403 and r.json()["error"]["code"] == "WAREHOUSE_ACCESS_DENIED"
    body["warehouse_id"] = w["wh1"]
    assert client.post("/api/inventory/adjust", headers=h["manager"], json=body).status_code == 200
    assert client.get("/api/inventory", headers=h["customer"]).status_code == 403
    assert client.get(f"/api/warehouses/{w['wh2']}", headers=h["manager"]).status_code == 403


def test_product_crud_and_duplicate_sku(client, world):
    h = world["h"]["admin"]
    dup = client.post("/api/products", headers=h, json={"sku": "SKU-1001", "name": "d", "price": "1", "cost_price": "1"})
    assert dup.status_code == 409 and dup.json()["error"]["code"] == "PRODUCT_SKU_EXISTS"
    pid = world["pid"]
    r = client.patch(f"/api/products/{pid}", headers=h, json={"name": "Widget 2", "price": "12.50"})
    assert r.json()["name"] == "Widget 2" and r.json()["category"] == "Tools"
    assert client.post(f"/api/products/{pid}/deactivate", headers=h).json()["is_active"] is False
    assert client.get("/api/products", headers=world["h"]["customer"]).json()["total"] == 0
    assert client.post(f"/api/products/{pid}/activate", headers=h).json()["is_active"] is True
    assert client.get(f"/api/products/{pid}", headers=h).status_code == 200


def test_adjustment_records_immutable_transaction(client, world):
    stock(client, world, 100)
    r = client.post("/api/inventory/adjust", headers=world["h"]["admin"],
                    json={"product_id": world["pid"], "warehouse_id": world["wh1"], "quantity": -30, "reason": "recount"})
    assert r.json()["available_quantity"] == 70
    tx = client.get("/api/inventory/transactions", headers=world["h"]["admin"]).json()["items"][0]
    assert (tx["type"], tx["previous_quantity"], tx["new_quantity"], tx["quantity"], tx["reason"]) == ("ADJUSTMENT", 100, 70, -30, "recount")
    assert client.patch("/api/inventory/transactions/1", headers=world["h"]["admin"]).status_code in (404, 405)


def test_negative_stock_rejected(client, world):
    stock(client, world, 5)
    r = client.post("/api/inventory/adjust", headers=world["h"]["admin"],
                    json={"product_id": world["pid"], "warehouse_id": world["wh1"], "quantity": -6, "reason": "oops"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    assert inv(client, world)["available_quantity"] == 5


def test_transfer_success_and_two_transactions(client, world):
    stock(client, world, 100)
    body = {"product_id": world["pid"], "from_warehouse_id": world["wh1"], "to_warehouse_id": world["wh2"], "quantity": 20}
    r = client.post("/api/inventory/transfer", headers=world["h"]["admin"], json=body)
    assert r.status_code == 200
    assert inv(client, world)["available_quantity"] == 80 and inv(client, world, world["wh2"])["available_quantity"] == 20
    types = {t["type"] for t in client.get("/api/inventory/transactions", headers=world["h"]["admin"]).json()["items"]}
    assert {"TRANSFER_OUT", "TRANSFER_IN"} <= types


def test_transfer_failures_leave_inventory_unchanged(client, world):
    stock(client, world, 10)
    base = {"product_id": world["pid"], "from_warehouse_id": world["wh1"], "to_warehouse_id": world["wh2"]}
    r = client.post("/api/inventory/transfer", headers=world["h"]["admin"], json={**base, "quantity": 50})
    assert r.status_code == 409 and r.json()["error"]["code"] == "INSUFFICIENT_INVENTORY"
    r = client.post("/api/inventory/transfer", headers=world["h"]["admin"], json={**base, "to_warehouse_id": world["wh1"], "quantity": 1})
    assert r.json()["error"]["code"] == "INVALID_INVENTORY_TRANSFER"
    assert inv(client, world)["available_quantity"] == 10
    assert client.get("/api/inventory", headers=world["h"]["admin"], params={"warehouse_id": world["wh2"]}).json()["total"] == 0


def test_transfer_idempotency(client, world):
    stock(client, world, 100)
    h = {**world["h"]["admin"], "Idempotency-Key": "t-1"}
    body = {"product_id": world["pid"], "from_warehouse_id": world["wh1"], "to_warehouse_id": world["wh2"], "quantity": 20}
    a, b = client.post("/api/inventory/transfer", headers=h, json=body), client.post("/api/inventory/transfer", headers=h, json=body)
    assert a.json() == b.json() and inv(client, world)["available_quantity"] == 80


def test_low_stock_detection_and_notification(client, world):
    stock(client, world, 50)
    client.post("/api/inventory/adjust", headers=world["h"]["admin"],
                json={"product_id": world["pid"], "warehouse_id": world["wh1"], "quantity": -45, "reason": "x", "reorder_level": 10})
    low = client.get("/api/inventory/low-stock", headers=world["h"]["manager"]).json()
    assert low["total"] == 1 and low["items"][0]["available_quantity"] == 5
    assert client.get("/api/inventory", headers=world["h"]["admin"], params={"low_stock": "true"}).json()["total"] == 1
    notes = client.get("/api/notifications", headers=world["h"]["manager"]).json()["items"]
    assert any(n["type"] == "LOW_STOCK" for n in notes)
    assert client.get("/api/inventory", headers=world["h"]["admin"], params={"out_of_stock": "true"}).json()["total"] == 0
