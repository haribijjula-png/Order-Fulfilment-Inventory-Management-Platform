from datetime import timedelta
from app.core.security import make_token
from tests.conftest import PW, hdr, login, mk_user
from app.models import CUSTOMER


def test_register_login_me(client):
    r = client.post("/api/auth/register", json={"email": "a@b.io", "password": PW, "full_name": "A"})
    assert r.status_code == 201 and r.json()["role"] == "CUSTOMER"
    assert client.post("/api/auth/register", json={"email": "a@b.io", "password": PW, "full_name": "A"}).status_code == 409
    me = client.get("/api/auth/me", headers=hdr(client, "a@b.io"))
    assert me.json()["email"] == "a@b.io"


def test_invalid_credentials_and_error_shape(client, Session):
    mk_user(Session, CUSTOMER, "c@t.io")
    r = client.post("/api/auth/login", json={"email": "c@t.io", "password": "wrong-pass"})
    assert r.status_code == 401
    assert r.json() == {"success": False, "error": {"code": "INVALID_CREDENTIALS", "message": "Invalid email or password"}}


def test_validation_error_422(client):
    r = client.post("/api/auth/register", json={"email": "bad", "password": "x"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "VALIDATION_ERROR"


def test_refresh_rotation_and_reuse_detection(client, Session):
    mk_user(Session, CUSTOMER, "c@t.io")
    t1 = login(client, "c@t.io")
    t2 = client.post("/api/auth/refresh", json={"refresh_token": t1["refresh_token"]}).json()
    assert t2["refresh_token"] != t1["refresh_token"]
    reuse = client.post("/api/auth/refresh", json={"refresh_token": t1["refresh_token"]})
    assert reuse.status_code == 401 and reuse.json()["error"]["code"] == "TOKEN_REVOKED"
    # whole family revoked after reuse
    assert client.post("/api/auth/refresh", json={"refresh_token": t2["refresh_token"]}).status_code == 401


def test_logout_revokes_refresh_token(client, Session):
    mk_user(Session, CUSTOMER, "c@t.io")
    t = login(client, "c@t.io")
    assert client.post("/api/auth/logout", json={"refresh_token": t["refresh_token"]}).status_code == 204
    assert client.post("/api/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401


def test_expired_and_wrong_type_tokens(client, Session):
    uid = mk_user(Session, CUSTOMER, "c@t.io")
    expired, _, _ = make_token(uid, "access", timedelta(seconds=-5))
    r = client.get("/api/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "TOKEN_EXPIRED"
    refresh = login(client, "c@t.io")["refresh_token"]
    assert client.get("/api/auth/me", headers={"Authorization": f"Bearer {refresh}"}).status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_inactive_user_cannot_authenticate(client, Session):
    mk_user(Session, CUSTOMER, "c@t.io")
    h = hdr(client, "c@t.io")
    mk_user(Session, CUSTOMER, "off@t.io", active=False)
    r = client.post("/api/auth/login", json={"email": "off@t.io", "password": PW})
    assert r.status_code == 403 and r.json()["error"]["code"] == "ACCOUNT_INACTIVE"
    assert client.get("/api/auth/me", headers=h).status_code == 200


def test_change_password_revokes_tokens(client, Session):
    mk_user(Session, CUSTOMER, "c@t.io")
    t = login(client, "c@t.io")
    h = {"Authorization": f"Bearer {t['access_token']}"}
    assert client.post("/api/auth/change-password", headers=h, json={"old_password": "bad", "new_password": "NewPassw0rd!"}).status_code == 401
    assert client.post("/api/auth/change-password", headers=h, json={"old_password": PW, "new_password": "NewPassw0rd!"}).status_code == 204
    assert client.post("/api/auth/refresh", json={"refresh_token": t["refresh_token"]}).status_code == 401
    login(client, "c@t.io", "NewPassw0rd!")
