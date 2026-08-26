"""Push subscription endpoints: auth, and the off switch.

A subscription endpoint is a delivery capability for a specific device, so
these routes must never be reachable without a session, and must refuse
cleanly — not 500 — on a server with no VAPID keys.
"""

import pytest
from fastapi.testclient import TestClient

SUB = {
    "endpoint": "https://fcm.googleapis.com/fcm/send/abc123",
    "keys": {"p256dh": "BPk3…", "auth": "cXf9…"},
    # Browsers send more than this; extra fields must not 422 the request.
    "expirationTime": None,
}


@pytest.fixture()
def client(monkeypatch):
    # A realistic value: the app now refuses to start on a shipped
    # placeholder or a one-character secret, and these tests are about
    # authorisation, not about secret strength.
    monkeypatch.setenv("CELMIS_JWT_SECRET", "e3f1a9c7d2b48065f1ac93de77208b4c")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-12345678")
    # Never reached by these tests — auth is resolved first — but the app
    # refuses to build without it.
    monkeypatch.setenv("DATABASE_URL",
                       "postgresql+asyncpg://u:p@localhost:5432/celmis_test")
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("VAPID_PRIVATE_KEY", raising=False)
    from src.api.main import build_app
    return TestClient(build_app())


def test_every_route_requires_a_session(client):
    assert client.get("/api/push/config").status_code == 401
    assert client.post("/api/push/subscribe", json=SUB).status_code == 401
    assert client.request("DELETE", "/api/push/subscribe",
                          json={"endpoint": SUB["endpoint"]}).status_code == 401
    assert client.post("/api/push/test").status_code == 401


def test_routes_are_mounted(client):
    """A 404 here would mean the router was never included — the feature would
    look 'broken in the browser' rather than 'not wired up'.

    Probed by request, not by walking app.routes: this FastAPI version keeps
    included routers wrapped rather than flattened, so introspection reports
    an empty list even when the routes serve fine.
    """
    for method, path in (("GET", "/api/push/config"),
                         ("POST", "/api/push/subscribe"),
                         ("DELETE", "/api/push/subscribe"),
                         ("POST", "/api/push/test")):
        r = client.request(method, path, json=SUB)
        assert r.status_code != 404, f"{method} {path} is not mounted"


def test_extra_subscription_fields_are_tolerated():
    """PushSubscription.toJSON() carries fields we do not use; rejecting them
    would break every client the day a browser adds one."""
    from src.api.routers.push import SubscribeIn

    parsed = SubscribeIn.model_validate(SUB)
    assert parsed.endpoint.endswith("abc123")
    assert parsed.keys.p256dh == "BPk3…"


def test_subscription_requires_both_keys():
    from pydantic import ValidationError

    from src.api.routers.push import SubscribeIn

    with pytest.raises(ValidationError):
        SubscribeIn.model_validate({"endpoint": "https://x/y", "keys": {"p256dh": "a"}})
