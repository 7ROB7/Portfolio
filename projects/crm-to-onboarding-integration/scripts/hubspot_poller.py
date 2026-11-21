"""OPTIONAL Stage 3: pull real closed-won deals from a free HubSpot account and forward them
to the integration as signed webhook events.

Why polling instead of a HubSpot webhook? On HubSpot's free tier, the simplest reliable trigger
is the CRM Search API. Polling + a checkpoint is a legitimate integration pattern (many iPaaS
"Watch records" triggers work exactly like this). Know the trade-off for interviews:
webhooks = near real-time but you must handle retries/duplicates; polling = simpler, but adds
latency and spends API quota.

Setup (HubSpot free CRM):
  1. Settings > Integrations > Private Apps > Create. Scopes: crm.objects.deals.read,
     crm.objects.companies.read, crm.objects.contacts.read. Copy the access token into .env
     as HUBSPOT_TOKEN.
  2. (Optional) Create a deal property "onboarding_plan" (dropdown: Starter/Growth/Enterprise).
     Otherwise the plan is derived from the amount (see plan_from_amount).
  3. Create a few deals, associate a company + contact, move them to "Closed won".
  4. Run:  python -m scripts.hubspot_poller            (once)
           python -m scripts.hubspot_poller --loop 60  (every 60 s)

NOTE: written against HubSpot's documented CRM v3 API. Not tested against a live account
from where this was built; expect to adjust small things (property names, pipeline stage IDs).
"""
from __future__ import annotations

import argparse
import os
import time
from datetime import datetime, timezone

import httpx

from app.config import ROOT
from scripts.send_test_webhook import send
from app.config import get_settings

API = "https://api.hubapi.com"
CHECKPOINT = ROOT / "data" / "hubspot_checkpoint.txt"


class HubSpot:
    def __init__(self, token: str):
        self.client = httpx.Client(base_url=API, timeout=30, headers={"Authorization": f"Bearer {token}"})

    def request(self, method: str, path: str, **kw) -> dict:
        for attempt in range(5):
            r = self.client.request(method, path, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                wait = float(r.headers.get("Retry-After", 2 ** attempt))  # respect the rate limit
                print(f"  HubSpot {r.status_code}; waiting {wait:.0f}s")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        raise RuntimeError(f"HubSpot kept failing on {path}")

    def closed_won_since(self, since_ms: int) -> list[dict]:
        body = {
            "filterGroups": [{"filters": [
                {"propertyName": "dealstage", "operator": "EQ", "value": "closedwon"},
                {"propertyName": "hs_lastmodifieddate", "operator": "GT", "value": str(since_ms)},
            ]}],
            "properties": ["dealname", "amount", "closedate", "deal_currency_code", "onboarding_plan", "hubspot_owner_id"],
            "sorts": [{"propertyName": "hs_lastmodifieddate", "direction": "ASCENDING"}],
            "limit": 100,
        }
        results, after = [], None
        while True:  # pagination: keep following "paging.next.after"
            if after:
                body["after"] = after
            data = self.request("POST", "/crm/v3/objects/deals/search", json=body)
            results += data.get("results", [])
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                return results

    def first_associated(self, deal_id: str, to: str, props: str) -> dict:
        deal = self.request("GET", f"/crm/v3/objects/deals/{deal_id}", params={"associations": to})
        assoc = deal.get("associations", {}).get(to, {}).get("results", [])
        if not assoc:
            return {}
        return self.request("GET", f"/crm/v3/objects/{to}/{assoc[0]['id']}", params={"properties": props})["properties"]


def plan_from_amount(amount: float) -> str:
    return "Enterprise" if amount >= 75000 else "Growth" if amount >= 20000 else "Starter"


def to_event(hs: HubSpot, deal: dict) -> dict:
    p = deal["properties"]
    company = hs.first_associated(deal["id"], "companies", "name")
    contact = hs.first_associated(deal["id"], "contacts", "firstname,lastname,email")
    amount = float(p.get("amount") or 0)
    return {
        "event_id": f"hubspot-{deal['id']}-{p.get('hs_lastmodifieddate', '')}",
        "event_type": "deal.closed_won",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": "hubspot_poller",
        "deal": {
            "deal_id": f"HS-{deal['id']}",
            "deal_name": p.get("dealname") or "",
            "company_name": company.get("name") or "",
            "contact_name": f"{contact.get('firstname') or ''} {contact.get('lastname') or ''}".strip(),
            "contact_email": contact.get("email") or "",
            "amount": amount,
            "currency": p.get("deal_currency_code") or "USD",
            "close_date": (p.get("closedate") or "")[:10],
            "plan": p.get("onboarding_plan") or plan_from_amount(amount),
            "owner": p.get("hubspot_owner_id"),
        },
    }


def run_once(hs: HubSpot, url: str, secret: str) -> None:
    since = int(CHECKPOINT.read_text()) if CHECKPOINT.exists() else 0
    deals = hs.closed_won_since(since)
    print(f"{len(deals)} closed-won deal(s) changed since checkpoint {since}")
    newest = since
    with httpx.Client(timeout=60) as client:
        for d in deals:
            event = to_event(hs, d)
            r = send(client, url, secret, event)
            print(f"  {event['deal']['deal_id']}: HTTP {r.status_code} {r.json().get('status')}")
            if r.status_code >= 500:
                print("  Integration unavailable; stopping so the checkpoint doesn't skip this deal")
                break
            modified = d["properties"].get("hs_lastmodifieddate")
            if modified:
                newest = max(newest, int(datetime.fromisoformat(modified.replace("Z", "+00:00")).timestamp() * 1000))
    CHECKPOINT.parent.mkdir(exist_ok=True)
    CHECKPOINT.write_text(str(newest))


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000/webhooks/crm/deal-closed")
    p.add_argument("--loop", type=int, default=0, help="poll every N seconds")
    args = p.parse_args()
    token = os.getenv("HUBSPOT_TOKEN")
    if not token:
        raise SystemExit("Set HUBSPOT_TOKEN in .env (HubSpot private app access token)")
    hs = HubSpot(token)
    secret = get_settings().webhook_secret
    while True:
        run_once(hs, args.url, secret)
        if not args.loop:
            break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
