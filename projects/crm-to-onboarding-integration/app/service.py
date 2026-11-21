"""Orchestration: turn one closed-won deal into a customer + onboarding project in Odoo.

Design choice: RECONCILE, don't blindly CREATE.
Every step is "find it, create it only if missing". So if the service crashes halfway
(say after the project but before all tasks), simply re-sending the event finishes the
job without creating duplicates. That makes the whole flow safe to retry.
"""
from __future__ import annotations

import logging
from datetime import date

from .mapping import OnboardingPlan, build_onboarding_plan
from .models import Deal
from .odoo_client import BaseOdoo
from .store import Store

log = logging.getLogger("service")


def _iso(d: date) -> str:
    return d.isoformat()


class OnboardingService:
    def __init__(self, odoo: BaseOdoo, store: Store, template: dict):
        self.odoo = odoo
        self.store = store
        self.template = template

    # -- step helpers ---------------------------------------------------------------------
    def _find_or_create(self, model: str, domain: list, values: dict) -> tuple[int, bool]:
        found = self.odoo.search_read(model, domain, ["id"], limit=1)
        if found:
            return found[0]["id"], False
        return self.odoo.create(model, values), True

    def _ensure_company(self, deal: Deal) -> tuple[int, bool]:
        return self._find_or_create(
            "res.partner",
            [("name", "=", deal.company_name), ("is_company", "=", True)],
            {"name": deal.company_name, "is_company": True},
        )

    def _ensure_contact(self, deal: Deal, company_id: int) -> tuple[int, bool]:
        return self._find_or_create(
            "res.partner",
            [("email", "=", deal.contact_email)],
            {"name": deal.contact_name, "email": deal.contact_email, "parent_id": company_id, "is_company": False},
        )

    def _ensure_project(self, deal: Deal, plan: OnboardingPlan, company_id: int) -> tuple[int, bool]:
        description = (
            f"<p>Auto-created from CRM deal <b>{deal.deal_id}</b> ({deal.deal_name}).</p>"
            f"<ul><li>Plan: {deal.plan}</li><li>Contract value: {deal.amount:,.0f} {deal.currency}</li>"
            f"<li>Primary contact: {deal.contact_name} &lt;{deal.contact_email}&gt;</li>"
            f"<li>Target go-live: {_iso(plan.go_live)}</li><li>Sales owner: {deal.owner or '-'}</li></ul>"
        )
        return self._find_or_create(
            "project.project",
            [("name", "=", plan.project_name)],
            {
                "name": plan.project_name,
                "partner_id": company_id,
                "date_start": _iso(plan.kickoff),
                "date": _iso(plan.end_date),
                "description": description,
            },
        )

    def _ensure_stage(self, name: str, sequence: int, project_id: int) -> int:
        """Odoo task stages are shared records; link the stage to this project with the
        many2many command (4, id) = 'add this id to the relation'."""
        # user_id = False excludes Odoo 16+/17 *personal* stages (Inbox, Today...), which share this model
        found = self.odoo.search_read("project.task.type", [("name", "=", name), ("user_id", "=", False)], ["id"], limit=1)
        if found:
            stage_id = found[0]["id"]
            self.odoo.write("project.task.type", [stage_id], {"project_ids": [(4, project_id)]})
            return stage_id
        return self.odoo.create("project.task.type", {"name": name, "sequence": sequence, "project_ids": [(4, project_id)]})

    # -- main entry point -----------------------------------------------------------------
    def handle(self, deal: Deal) -> dict:
        plan = build_onboarding_plan(deal, self.template)
        company_id, company_new = self._ensure_company(deal)
        contact_id, contact_new = self._ensure_contact(deal, company_id)
        project_id, project_new = self._ensure_project(deal, plan, company_id)

        existing = {t["name"] for t in self.odoo.search_read(
            "project.task", [("project_id", "=", project_id)], ["name"])}

        created_tasks = 0
        for phase in plan.phases:
            stage_id = self._ensure_stage(phase.name, phase.sequence, project_id)
            for task in phase.tasks:
                if task.name in existing:
                    continue
                self.odoo.create("project.task", {
                    "name": task.name,
                    "project_id": project_id,
                    "stage_id": stage_id,
                    "date_deadline": _iso(task.deadline),
                    "partner_id": company_id,
                })
                created_tasks += 1

        self.store.upsert_link(deal.deal_id, company_id, contact_id, project_id, plan.task_count)
        anything_new = company_new or contact_new or project_new or created_tasks > 0
        result = {
            "status": "created" if project_new else ("repaired" if anything_new else "no_changes"),
            "deal_id": deal.deal_id,
            "project_id": project_id,
            "project_name": plan.project_name,
            "company_id": company_id,
            "contact_id": contact_id,
            "tasks_created": created_tasks,
            "tasks_expected": plan.task_count,
            "kickoff": _iso(plan.kickoff),
            "go_live": _iso(plan.go_live),
        }
        log.info("Deal %s -> project %s (%s, %d tasks created)", deal.deal_id, project_id, result["status"], created_tasks)
        return result
