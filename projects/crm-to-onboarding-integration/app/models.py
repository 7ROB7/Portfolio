"""The event contract this service accepts: one closed-won deal from the CRM."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PLANS = ("Starter", "Growth", "Enterprise")


class Deal(BaseModel):
    deal_id: str = Field(min_length=1)
    deal_name: str = Field(min_length=1)
    company_name: str = Field(min_length=1)
    contact_name: str = Field(min_length=1)
    contact_email: str
    amount: float = Field(ge=0)
    currency: str = "USD"
    close_date: date
    plan: Literal["Starter", "Growth", "Enterprise"]
    owner: str | None = None

    @field_validator("contact_email")
    @classmethod
    def email_must_be_valid(cls, value: str) -> str:
        value = value.strip().lower()
        if not EMAIL_RE.match(value):
            raise ValueError("not a valid email address")
        return value

    @field_validator("plan", mode="before")
    @classmethod
    def normalise_plan(cls, value):
        # CRMs are inconsistent ("enterprise", "ENTERPRISE "): normalise before validating.
        if isinstance(value, str):
            value = value.strip().title()
        return value

    @field_validator("company_name", "contact_name", "deal_name")
    @classmethod
    def strip_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value


class DealEvent(BaseModel):
    event_id: str = Field(min_length=1)        # unique per delivery attempt group; used for idempotency
    event_type: Literal["deal.closed_won"]
    occurred_at: datetime
    source: str = "crm"
    deal: Deal
