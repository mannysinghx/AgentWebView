"""The sandboxed browser with a real Chromium (skipped when Playwright or its Chromium is not installed)."""

from __future__ import annotations

import time
from typing import Any

import pytest
from conftest import UI, bearer, make_agent
from fastapi.testclient import TestClient

pytestmark = pytest.mark.browser


def _chromium_ready() -> bool:
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            return bool(p.chromium.executable_path) and __import__("os").path.exists(p.chromium.executable_path)
    except Exception:
        return False


@pytest.mark.skipif(not _chromium_ready(), reason="Playwright Chromium is not installed (playwright install chromium)")
def test_browser_session_waits_for_approval_on_risky_click(client: TestClient, site: dict[str, Any]) -> None:
    site["pages"]["/"] = (
        200,
        "text/html",
        "<html><head><title>Account</title></head><body><h1>Your account</h1>"
        "<p>Manage your subscription here.</p>"
        "<form method='post' action='/done'><button type='submit'>Delete my account</button></form>"
        "<img src='http://127.0.0.1:1/tracker.png'></body></html>",
    )
    client.patch("/api/settings", json={"browser": {"enabled": True}}, headers=UI)
    _agent, token = make_agent(client, abilities=["browser"])
    opened = client.post("/v1/browser/sessions", json={"start_url": "http://site.test/"}, headers=bearer(token))
    assert opened.status_code == 200, opened.text
    session = opened.json()
    assert session["state"] == "active", session
    assert session["actions"][0]["state"] == "succeeded" and session["actions"][0]["result"]["title"] == "Account"
    sid = session["id"]

    text = client.post(f"/v1/browser/sessions/{sid}/actions", json={"type": "extract", "mode": "main_text"}, headers=bearer(token)).json()
    assert text["state"] == "succeeded" and "subscription" in text["result"]["text"]

    click = client.post(
        f"/v1/browser/sessions/{sid}/actions?wait_ms=5000",
        json={"type": "click", "selector": {"role": "button", "name": "Delete my account"}},
        headers=bearer(token),
    ).json()
    assert click["state"] == "awaiting_decision" and click["write_category"] == "delete"
    pending = client.get("/api/approvals", headers=UI).json()["pending"]
    assert [p["id"] for p in pending] == [click["id"]]
    client.post(f"/api/approvals/{click['id']}", json={"approve": False, "note": "no"}, headers=UI)
    after = client.get(f"/v1/browser/actions/{click['id']}", headers=bearer(token)).json()
    assert after["state"] == "denied"

    shot = client.post(f"/v1/browser/sessions/{sid}/actions", json={"type": "screenshot", "inline": True}, headers=bearer(token)).json()
    assert shot["state"] == "succeeded" and shot["result"]["png_base64"]
    png = client.get(shot["screenshot_url"])
    assert png.status_code == 200 and png.content[:4] == b"\x89PNG"

    blocked = client.post(
        f"/v1/browser/sessions/{sid}/actions", json={"type": "navigate", "url": "http://other.example/"}, headers=bearer(token)
    ).json()
    assert blocked["state"] == "failed" and blocked["error"]["code"] == "NETWORK_BLOCKED"

    closed = client.delete(f"/v1/browser/sessions/{sid}", headers=bearer(token)).json()
    assert closed["state"] == "closed"
    receipt = None
    for _ in range(50):
        receipt = client.get(f"/v1/tasks/{session['task_id']}", headers=bearer(token)).json()
        if receipt["status"] != "running":
            break
        time.sleep(0.1)
    assert receipt is not None and receipt["status"] == "succeeded"
    assert int(receipt["resource_usage"]["browser_ms"]) > 0
