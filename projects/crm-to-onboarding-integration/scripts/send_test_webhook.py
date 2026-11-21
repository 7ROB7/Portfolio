"""Plays the role of the CRM: signs and sends closed-won deal events to the service.

Examples
    python -m scripts.send_test_webhook                  # send every closed-won deal in samples/deals.json
    python -m scripts.send_test_webhook --only HS-1001   # one deal
    python -m scripts.send_test_webhook --replay         # send everything twice (shows idempotency)
    python -m scripts.send_test_webhook --tamper         # wrong signature (shows 401)
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import ROOT, get_settings
from app.security import compute_signature


def build_event(deal: dict) -> dict:
    deal = {k: v for k, v in deal.items() if k != "stage"}
    return {
        "event_id": f"evt-{deal['deal_id']}-closedwon",
        "event_type": "deal.closed_won",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": "send_test_webhook",
        "deal": deal,
    }


def send(client: httpx.Client, url: str, secret: str, event: dict) -> httpx.Response:
    body = json.dumps(event).encode()
    ts = str(int(time.time()))
    headers = {"Content-Type": "application/json", "X-Timestamp": ts,
               "X-Signature": compute_signature(secret, ts, body)}
    return client.post(url, content=body, headers=headers)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", default="http://localhost:8000/webhooks/crm/deal-closed")
    p.add_argument("--file", default=str(ROOT / "samples" / "deals.json"))
    p.add_argument("--only", help="send just this deal_id")
    p.add_argument("--replay", action="store_true", help="send each event twice")
    p.add_argument("--tamper", action="store_true", help="sign with the wrong secret")
    args = p.parse_args()

    secret = "wrong-secret" if args.tamper else get_settings().webhook_secret
    deals = json.loads(Path(args.file).read_text(encoding="utf-8"))
    deals = [d for d in deals if d.get("stage", "closedwon") == "closedwon"]  # only closed-won deals trigger
    if args.only:
        deals = [d for d in deals if d["deal_id"] == args.only]

    print(f"{'deal':<9} {'HTTP':<5} {'status':<12} detail")
    print("-" * 78)
    with httpx.Client(timeout=60) as client:
        for deal in deals:
            event = build_event(deal)
            for _ in range(2 if args.replay else 1):
                r = send(client, args.url, secret, event)
                data = r.json()
                if r.status_code in (200, 201) and "project_id" in data:
                    detail = f"project #{data['project_id']}, {data['tasks_created']}/{data['tasks_expected']} tasks, go-live {data['go_live']}"
                elif "errors" in data:
                    detail = "; ".join(f"{e['field']}: {e['error']}" for e in data["errors"])
                else:
                    detail = data.get("error", "")
                print(f"{deal['deal_id']:<9} {r.status_code:<5} {data.get('status', ''):<12} {detail}")


if __name__ == "__main__":
    main()
