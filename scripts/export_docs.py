"""Generate docs/openapi.json and docs/postman_collection.json from the FastAPI app."""
import json, os, pathlib
os.environ.setdefault("DATABASE_URL", "sqlite://")
from app.main import app

spec = app.openapi()
out = pathlib.Path("docs"); out.mkdir(exist_ok=True)
(out / "openapi.json").write_text(json.dumps(spec, indent=2))


def example(schema, depth=0):
    if "$ref" in schema:
        schema = spec["components"]["schemas"][schema["$ref"].split("/")[-1]]
    if "anyOf" in schema:
        return example(next((s for s in schema["anyOf"] if s.get("type") != "null"), schema["anyOf"][0]), depth)
    t = schema.get("type")
    if "default" in schema and t != "object":
        return schema["default"]
    if t == "object" or "properties" in schema:
        return {k: example(v, depth + 1) for k, v in schema.get("properties", {}).items()} if depth < 4 else {}
    if t == "array":
        return [example(schema.get("items", {}), depth + 1)]
    return {"integer": 1, "number": 1, "boolean": True}.get(t, "string")


folders = {}
for path, methods in spec["paths"].items():
    for method, op in methods.items():
        tag = (op.get("tags") or ["misc"])[0]
        url = "{{base_url}}" + path.replace("{", ":").replace("}", "")
        req = {"method": method.upper(), "header": [], "url": {"raw": url, "host": ["{{base_url}}"],
               "path": [p.replace("{", ":").replace("}", "") for p in path.strip("/").split("/")]}}
        if "/orders" in path and method == "post" or "adjust" in path or "transfer" in path:
            req["header"].append({"key": "Idempotency-Key", "value": "{{$guid}}"})
        body = op.get("requestBody", {}).get("content", {}).get("application/json", {}).get("schema")
        if body:
            req["header"].append({"key": "Content-Type", "value": "application/json"})
            req["body"] = {"mode": "raw", "raw": json.dumps(example(body), indent=2)}
        if not path.startswith("/api/auth/login") and not path.startswith("/api/auth/register") and path != "/health":
            req["auth"] = {"type": "bearer", "bearer": [{"key": "token", "value": "{{access_token}}", "type": "string"}]}
        folders.setdefault(tag, []).append({"name": f"{method.upper()} {path}", "request": req})

login_test = ("const j = pm.response.json(); if (j.access_token) { pm.collectionVariables.set('access_token', j.access_token);"
              " pm.collectionVariables.set('refresh_token', j.refresh_token); }")
for item in folders.get("auth", []):
    if item["name"].endswith("/login") or item["name"].endswith("/refresh"):
        item["event"] = [{"listen": "test", "script": {"type": "text/javascript", "exec": [login_test]}}]
coll = {"info": {"name": "Order Fulfillment & Inventory API",
                 "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"},
        "variable": [{"key": "base_url", "value": "http://localhost:8000"}, {"key": "access_token", "value": ""},
                     {"key": "refresh_token", "value": ""}],
        "item": [{"name": t, "item": i} for t, i in folders.items()]}
(out / "postman_collection.json").write_text(json.dumps(coll, indent=2))
print("exported", sum(len(v) for v in folders.values()), "requests")
