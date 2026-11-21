"""Field mapping: a CRM deal -> an onboarding project plan (phases, tasks, dates).

Kept separate from any API code so the business rules can be unit-tested and
explained on their own ("what does a closed-won Enterprise deal turn into?").
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from .models import Deal

DEAL_REF_RE = re.compile(r"\[([^\]]+)\]\s*$")


def project_name_for(deal: Deal) -> str:
    # The deal ID is embedded in the name so the project can always be traced back
    # to its source record (used for idempotency and reconciliation).
    return f"{deal.company_name} - Onboarding [{deal.deal_id}]"


def deal_ref_from_project_name(name: str) -> str | None:
    match = DEAL_REF_RE.search(name or "")
    return match.group(1) if match else None


def add_business_days(start: date, days: int) -> date:
    """Move forward `days` working days (Mon-Fri). 0 = same day, rolled to Monday if on a weekend."""
    current = start
    while current.weekday() >= 5:
        current += timedelta(days=1)
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


@dataclass
class TaskSpec:
    name: str
    phase: str
    deadline: date


@dataclass
class PhaseSpec:
    name: str
    sequence: int
    tasks: list[TaskSpec] = field(default_factory=list)


@dataclass
class OnboardingPlan:
    project_name: str
    kickoff: date
    go_live: date
    end_date: date
    phases: list[PhaseSpec]

    @property
    def task_count(self) -> int:
        return sum(len(p.tasks) for p in self.phases)


def load_template(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def expected_task_count(template: dict, plan: str) -> int:
    return sum(1 for ph in template["phases"] for t in ph["tasks"] if plan in t["plans"])


def build_onboarding_plan(deal: Deal, template: dict) -> OnboardingPlan:
    kickoff = add_business_days(deal.close_date, template.get("kickoff_after_business_days", 3))
    phases: list[PhaseSpec] = []
    for seq, phase in enumerate(template["phases"], start=1):
        tasks = [
            TaskSpec(name=t["name"], phase=phase["name"], deadline=add_business_days(kickoff, t["due_day"]))
            for t in phase["tasks"]
            if deal.plan in t["plans"]
        ]
        if tasks:  # a Starter customer simply doesn't get an Integrations phase
            phases.append(PhaseSpec(name=phase["name"], sequence=seq, tasks=tasks))
    all_deadlines = [t.deadline for p in phases for t in p.tasks]
    golive_deadlines = [t.deadline for p in phases if p.name == "Go-Live" for t in p.tasks]
    go_live = min(golive_deadlines) if golive_deadlines else max(all_deadlines)  # the cutover date
    return OnboardingPlan(
        project_name=project_name_for(deal),
        kickoff=kickoff,
        go_live=go_live,
        end_date=max(all_deadlines),  # includes hypercare and handover
        phases=phases,
    )
