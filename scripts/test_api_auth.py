"""The API must not be drivable without the token once one is set.

These use FastAPI's TestClient, so they exercise the real routing and the real
dependency -- no mocks of the auth layer itself.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_api_auth.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402
from app.security import assert_binding_is_safe, is_loopback  # noqa: E402

failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"  ok   {name}")
    else:
        print(f"  FAIL {name} {detail}")
        failures.append(name)


def with_token(value: str | None):
    """Context manager-ish helper: set/unset REXENIOT_API_TOKEN."""
    class _Ctx:
        def __enter__(self):
            self.previous = os.environ.get("REXENIOT_API_TOKEN")
            if value is None:
                os.environ.pop("REXENIOT_API_TOKEN", None)
            else:
                os.environ["REXENIOT_API_TOKEN"] = value

        def __exit__(self, *exc):
            if self.previous is None:
                os.environ.pop("REXENIOT_API_TOKEN", None)
            else:
                os.environ["REXENIOT_API_TOKEN"] = self.previous

    return _Ctx()


TOKEN = "s3cret-token-for-tests"

#: Endpoints that must be closed once a token is set. (method, path)
PROTECTED = [
    ("GET", "/api/status"),
    ("POST", "/api/chat"),
    ("POST", "/api/chat/stream"),
    ("POST", "/api/speak"),
    ("POST", "/api/uploads"),
    ("GET", "/api/voices"),
    ("GET", "/api/admin/config"),
    ("GET", "/api/admin/hardware"),
    ("POST", "/api/admin/verify"),
]

print("no token configured (loopback default): the API stays open")
with with_token(None):
    client = TestClient(create_app())
    for method, path in PROTECTED:
        res = client.request(method, path, json={})
        check(f"{method} {path} is reachable without a token", res.status_code != 401,
              f"got {res.status_code}")

print("token configured: the API is closed")
with with_token(TOKEN):
    client = TestClient(create_app())
    for method, path in PROTECTED:
        res = client.request(method, path, json={})
        check(f"{method} {path} rejects a missing token", res.status_code == 401,
              f"got {res.status_code}")

        res = client.request(method, path, json={}, headers={"X-API-Token": "wrong"})
        check(f"{method} {path} rejects a wrong token", res.status_code == 401,
              f"got {res.status_code}")

        res = client.request(method, path, json={}, headers={"X-API-Token": TOKEN})
        check(f"{method} {path} accepts the right token", res.status_code != 401,
              f"got {res.status_code}")

print("liveness stays open so watchdogs can probe it")
with with_token(TOKEN):
    client = TestClient(create_app())
    res = client.get("/api/health")
    check("GET /api/health needs no token", res.status_code == 200, f"got {res.status_code}")
    check("/api/health advertises requires_token", res.json().get("requires_token") is True)
    check("/api/health does not leak the token", TOKEN not in res.text)

with with_token(None):
    client = TestClient(create_app())
    check("/api/health says no token needed", client.get("/api/health").json().get("requires_token") is False)

print("the admin session probe stays open, without leaking the token")
with with_token(TOKEN):
    client = TestClient(create_app())
    res = client.get("/api/admin/session")
    check("GET /api/admin/session needs no token", res.status_code == 200, f"got {res.status_code}")
    check("it reports the API token is required", res.json().get("requires_api_token") is True)
    check("it does not leak the token", TOKEN not in res.text)

print("an exposed server with only an admin token still needs the API token")
# The failure this guards: setting ADMIN_TOKEN and binding 0.0.0.0 would leave
# the agent itself (chat, tools) open, since the admin token only covers writes.
with with_token(TOKEN):
    client = TestClient(create_app())
    res = client.post("/api/chat", json={"message": "hi"}, headers={"X-Admin-Token": "whatever"})
    check("the admin token does not unlock chat", res.status_code == 401, f"got {res.status_code}")

print("binding safety")
check("127.0.0.1 is loopback", is_loopback("127.0.0.1"))
check("::1 is loopback", is_loopback("::1"))
check("0.0.0.0 is not loopback", not is_loopback("0.0.0.0"))
check("a blank host is not loopback", not is_loopback(""))
check("localhost is loopback", is_loopback("localhost"))
check("a LAN address is not loopback", not is_loopback("192.168.1.10"))
check("a hostname is not assumed loopback", not is_loopback("mybox.local"))

with with_token(None):
    try:
        assert_binding_is_safe("0.0.0.0", 8070)
        check("exposed bind without a token is refused", False, "no SystemExit raised")
    except SystemExit:
        check("exposed bind without a token is refused", True)

    assert_binding_is_safe("127.0.0.1", 8070)
    check("loopback bind without a token is allowed", True)

with with_token(TOKEN):
    assert_binding_is_safe("0.0.0.0", 8070)
    check("exposed bind with a token is allowed", True)

print()
if failures:
    print(f"{len(failures)} check(s) FAILED:")
    for name in failures:
        print(f"  - {name}")
    sys.exit(1)
print("all checks passed")
