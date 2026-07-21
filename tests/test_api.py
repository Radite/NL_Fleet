"""
End-to-end tests against the FastAPI app, running in mock mode (no
ANTHROPIC_API_KEY set) so these run offline in CI. They exercise the full
request path: prompt -> validate -> execute -> response.
"""

import os
os.environ.pop("ANTHROPIC_API_KEY", None)  # force mock mode for these tests
os.environ.pop("APP_API_KEY", None)  # default: no auth required for these tests

from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


def test_warranty_question_returns_ok_with_sql_and_rows():
    resp = client.post("/ask", json={"question": "which devices are expiring soon on warranty?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "SELECT" in body["sql"].upper()
    assert body["explanation"]
    assert isinstance(body["rows"], list)


def test_stale_checkin_question():
    resp = client.post("/ask", json={"question": "which devices haven't checked in for 30 days?"})
    body = resp.json()
    assert body["status"] == "ok"
    assert "last_checkin" in body["sql"]


def test_delete_style_question_is_declined_not_executed():
    resp = client.post("/ask", json={"question": "delete all devices in Provo"})
    body = resp.json()
    assert body["status"] == "declined"
    assert body["sql"] is None


def test_unrecognized_question_declines_visibly_rather_than_guessing():
    resp = client.post("/ask", json={"question": "what is the meaning of life"})
    body = resp.json()
    assert body["status"] == "declined"


def test_group_by_count_question():
    resp = client.post("/ask", json={"question": "how many devices per site?"})
    body = resp.json()
    assert body["status"] == "ok"
    assert body["row_count"] > 0
    assert "site" in [c.lower() for c in body["columns"]]


def test_empty_question_rejected_by_request_validation():
    resp = client.post("/ask", json={"question": ""})
    assert resp.status_code == 422  # pydantic min_length


def test_overlong_question_rejected_by_request_validation():
    resp = client.post("/ask", json={"question": "a" * 501})
    assert resp.status_code == 422  # pydantic max_length


def test_api_key_required_when_configured(monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module, "APP_API_KEY", "secret123")

    # Missing key -> rejected
    resp = client.post("/ask", json={"question": "how many devices per site?"})
    assert resp.status_code == 401

    # Wrong key -> rejected
    resp = client.post(
        "/ask",
        json={"question": "how many devices per site?"},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401

    # Correct key -> allowed
    resp = client.post(
        "/ask",
        json={"question": "how many devices per site?"},
        headers={"X-API-Key": "secret123"},
    )
    assert resp.status_code == 200


def test_easy_auth_header_identifies_client_and_skips_api_key_check(monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module, "APP_API_KEY", "secret123")

    # No API key header, but an Easy Auth identity header is present ->
    # treated as authenticated (App Service would already have gated this
    # request before it reached us).
    resp = client.post(
        "/ask",
        json={"question": "how many devices per site?"},
        headers={"X-MS-CLIENT-PRINCIPAL-NAME": "jsmith@pelicanenergy.example"},
    )
    assert resp.status_code == 200


def test_rate_limit_blocks_after_threshold(monkeypatch):
    import app.main as main_module
    monkeypatch.setattr(main_module, "RATE_LIMIT_MAX_REQUESTS", 3)
    main_module._request_log.clear()

    for _ in range(3):
        resp = client.post("/ask", json={"question": "how many devices per site?"})
        assert resp.status_code == 200

    resp = client.post("/ask", json={"question": "how many devices per site?"})
    assert resp.status_code == 429
