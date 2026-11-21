"""HTTP layer: the webhook endpoint the CRM (or Make/Zapier) calls.

Run:  uvicorn app.main:create_app --factory --reload
Docs: http://localhost:8000/docs  (auto-generated OpenAPI / Swagger UI)
"""
from __future__ import annotations

import json
import logging
import uuid

from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import Settings, get_settings
from .mapping import load_template
from .models import DealEvent
from .odoo_client import BaseOdoo, OdooError, TransientError, build_odoo
from .security import SignatureError, verify_signature
from .service import OnboardingService
from .store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
log = logging.getLogger("webhook")


def create_app(settings: Settings | None = None, odoo: BaseOdoo | None = None) -> FastAPI:
    settings = settings or get_settings()
    store = Store(settings.db_path)
    template = load_template(settings.template_path)
    odoo = odoo or build_odoo(settings)
    service = OnboardingService(odoo, store, template)

    app = FastAPI(
        title="CRM -> Onboarding Integration",
        description="Turns closed-won CRM deals into onboarding projects in Odoo.",
        version="1.0.0",
    )
    app.state.store, app.state.odoo, app.state.service = store, odoo, service

    @app.get("/health")
    def health():
        return {"status": "ok", "odoo_mode": settings.odoo_mode}

    @app.post("/webhooks/crm/deal-closed", status_code=201)
    async def deal_closed(
        request: Request,
        x_signature: str | None = Header(default=None),
        x_timestamp: str | None = Header(default=None),
    ):
        raw = await request.body()

        # 1) Authenticate: is this really from our CRM? (401 = don't retry, fix the secret)
        try:
            verify_signature(settings.webhook_secret, x_timestamp, raw, x_signature, settings.signature_tolerance)
        except SignatureError as exc:
            log.warning("Rejected unsigned/invalid request: %s", exc)
            return JSONResponse(status_code=401, content={"status": "unauthorized", "error": str(exc)})

        # 2) Parse + validate the payload. Bad data goes to the dead-letter list (status=rejected)
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return JSONResponse(status_code=400, content={"status": "bad_request", "error": "Body is not valid JSON"})
        try:
            event = DealEvent.model_validate(payload)
        except ValidationError as exc:
            errors = [{"field": ".".join(str(p) for p in e["loc"]), "error": e["msg"]} for e in exc.errors()]
            event_id = payload.get("event_id") if isinstance(payload, dict) else None
            deal_id = (payload.get("deal") or {}).get("deal_id") if isinstance(payload, dict) else None
            store.record_rejected(event_id or f"rejected-{uuid.uuid4()}", deal_id, raw.decode(errors="replace"),
                                  json.dumps(errors))
            log.warning("Rejected event %s (deal %s): %s", event_id, deal_id, errors)
            return JSONResponse(status_code=422, content={"status": "rejected", "event_id": event_id, "errors": errors})

        # 3) Idempotency: the same event delivered twice must not create two projects
        state = store.begin_event(event.event_id, event.deal.deal_id, raw.decode())
        if state == "duplicate":
            return JSONResponse(status_code=200, content={"status": "duplicate", "event_id": event.event_id})
        if state == "in_progress":
            return JSONResponse(status_code=409, content={"status": "in_progress", "event_id": event.event_id})

        # 4) Do the work (blocking XML-RPC calls run in a worker thread)
        try:
            result = await run_in_threadpool(service.handle, event.deal)
        except TransientError as exc:
            store.finish_event(event.event_id, "failed", error=f"transient: {exc}")
            # 503 tells the sender "try again later". Most webhook senders retry on 5xx.
            return JSONResponse(status_code=503, content={"status": "failed", "retryable": True, "error": str(exc)})
        except OdooError as exc:
            store.finish_event(event.event_id, "failed", error=str(exc))
            return JSONResponse(status_code=502, content={"status": "failed", "retryable": False, "error": str(exc)})

        store.finish_event(event.event_id, "success", result=result)
        code = 201 if result["status"] == "created" else 200
        return JSONResponse(status_code=code, content={"event_id": event.event_id, **result})

    @app.get("/events")
    def events(limit: int = 50):
        """Audit log + dead-letter view. (In production you'd put auth on this.)"""
        return store.list_events(limit)

    return app
