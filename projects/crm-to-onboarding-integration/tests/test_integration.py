"""Run with:  pytest -v"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from datetime import date

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import create_app
from app.mapping import add_business_days, build_onboarding_plan, expected_task_count, load_template
from app.models import Deal
from app.odoo_client import MockOdoo
from app.security import compute_signature

SECRET = "test-secret"
URL = "/webhooks/crm/deal-closed"


def make_event(deal_id="D-100", plan="Enterprise", email="priya@acme.example", event_id=None, **deal_overrides):
    deal = {
        "deal_id": deal_id, "deal_name": f"Acme {plan}", "company_name": "Acme Corp",
        "contact_name": "Priya Raman", "contact_email": email, "amount": 48000, "currency": "USD",
        "close_date": "2026-11-20", "plan": plan, "owner": "sales.rep@vendor.example",
    }
    deal.update(deal_overrides)
    return {"event_id": event_id or f"evt-{deal_id}", "event_type": "deal.closed_won",
            "occurred_at": "2026-11-20T09:30:00Z", "source": "test", "deal": deal}


def signed_headers(body: bytes, secret=SECRET, ts=None):
    ts = str(int(ts if ts is not None else time.time()))
    return {"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": compute_signature(secret, ts, body)}


@pytest.fixture
def ctx(tmp_path):
    settings = replace(get_settings(), webhook_secret=SECRET, db_path=tmp_path / "int.db",
                       mock_odoo_path=tmp_path / "odoo.db", odoo_mode="mock")
    odoo = MockOdoo(settings.mock_odoo_path, sleep=lambda s: None)  # no real waiting in tests
    app = create_app(settings, odoo=odoo)
    return TestClient(app), odoo


def post(client, payload, **kw):
    body = json.dumps(payload).encode()
    return client.post(URL, content=body, headers=signed_headers(body, **kw))


# ---------- mapping rules ----------
def test_business_days_skip_weekends():
    assert add_business_days(date(2026, 11, 20), 1) == date(2026, 11, 23)   # Fri + 1 -> Mon
    assert add_business_days(date(2026, 11, 21), 0) == date(2026, 11, 23)   # Sat rolls to Mon


def test_plan_size_differs_by_tier():
    template = load_template(get_settings().template_path)
    starter = build_onboarding_plan(Deal(**make_event(plan="Starter")["deal"]), template)
    enterprise = build_onboarding_plan(Deal(**make_event(plan="Enterprise")["deal"]), template)
    assert starter.task_count == expected_task_count(template, "Starter")
    assert enterprise.task_count > starter.task_count
    assert "Integrations" not in [p.name for p in starter.phases]
    assert starter.kickoff == date(2026, 11, 25)  # close Fri 20 Nov + 3 business days


# ---------- happy path ----------
def test_closed_won_creates_customer_project_and_tasks(ctx):
    client, odoo = ctx
    r = post(client, make_event())
    assert r.status_code == 201, r.text
    data = r.json()
    assert data["status"] == "created"
    assert data["tasks_created"] == data["tasks_expected"]
    projects = odoo.search_read("project.project", [], ["name", "partner_id"])
    assert projects[0]["name"] == "Acme Corp - Onboarding [D-100]"
    assert projects[0]["partner_id"][1] == "Acme Corp"


# ---------- idempotency ----------
def test_same_event_twice_is_a_duplicate(ctx):
    client, odoo = ctx
    post(client, make_event())
    r = post(client, make_event())
    assert r.status_code == 200 and r.json()["status"] == "duplicate"
    assert len(odoo.search_read("project.project", [], ["id"])) == 1


def test_new_event_for_same_deal_changes_nothing(ctx):
    client, odoo = ctx
    post(client, make_event(event_id="evt-1"))
    r = post(client, make_event(event_id="evt-2"))
    assert r.json()["status"] == "no_changes"
    assert len(odoo.search_read("project.project", [], ["id"])) == 1


# ---------- security ----------
def test_bad_signature_is_rejected(ctx):
    client, _ = ctx
    body = json.dumps(make_event()).encode()
    r = client.post(URL, content=body, headers=signed_headers(body, secret="wrong-secret"))
    assert r.status_code == 401


def test_old_timestamp_is_rejected_as_replay(ctx):
    client, _ = ctx
    r = post(client, make_event(), ts=time.time() - 3600)
    assert r.status_code == 401 and "replay" in r.json()["error"]


def test_tampered_body_is_rejected(ctx):
    client, _ = ctx
    body = json.dumps(make_event()).encode()
    headers = signed_headers(body)
    r = client.post(URL, content=body.replace(b"48000", b"1"), headers=headers)
    assert r.status_code == 401


# ---------- validation / dead letter ----------
def test_invalid_email_is_rejected_and_logged(ctx):
    client, odoo = ctx
    r = post(client, make_event(email="not-an-email"))
    assert r.status_code == 422
    assert r.json()["errors"][0]["field"] == "deal.contact_email"
    events = client.get("/events").json()
    assert events[0]["status"] == "rejected"
    assert odoo.search_read("project.project", [], ["id"]) == []


# ---------- resilience ----------
def test_transient_errors_are_retried(ctx):
    client, odoo = ctx
    odoo.fail_next = 3  # three simulated 429s, then success
    r = post(client, make_event())
    assert r.status_code == 201
    assert odoo.retry_count == 3


def test_gives_up_after_max_retries_then_recovers_on_resend(ctx):
    client, odoo = ctx
    odoo.fail_next = 100
    r = post(client, make_event())
    assert r.status_code == 503 and r.json()["retryable"] is True
    odoo.fail_next = 0
    r = post(client, make_event())  # same event_id: previous attempt failed, so it is retried
    assert r.status_code == 201


def test_partial_failure_is_repaired_without_duplicates(ctx):
    client, odoo = ctx
    template = load_template(get_settings().template_path)
    total = expected_task_count(template, "Enterprise")
    # Let the first few calls succeed, then fail permanently mid-way through task creation
    original = odoo._execute
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if 12 < calls["n"] < 100:
            from app.odoo_client import TransientError
            raise TransientError("network down")
        return original(*a, **k)

    odoo._execute = flaky
    assert post(client, make_event()).status_code == 503
    odoo._execute = original  # network is back
    partial = len(odoo.search_read("project.task", [], ["id"]))
    assert 0 < partial < total

    r = post(client, make_event())
    assert r.status_code == 200 and r.json()["status"] == "repaired"
    assert len(odoo.search_read("project.task", [], ["id"])) == total
    assert len(odoo.search_read("project.project", [], ["id"])) == 1
