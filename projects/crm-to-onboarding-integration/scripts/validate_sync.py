"""Reconciliation: did every closed-won deal in the CRM land correctly in Odoo?

Pulls data from both sides, loads it into an in-memory SQLite database, and runs plain SQL
checks: the same thing you'd do after a data migration or when a customer says
"some of our projects are missing".

    python -m scripts.validate_sync                      # source = samples/deals.json
    python -m scripts.validate_sync --source export.json # e.g. a CRM export

Writes reports/reconciliation_<timestamp>.md and exits with code 1 if anything is wrong
(so it can run in CI or on a schedule).
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from app.config import ROOT, get_settings
from app.mapping import deal_ref_from_project_name, expected_task_count, load_template
from app.odoo_client import build_odoo

CHECKS = {
    "Closed-won deals with NO onboarding project": """
        SELECT s.deal_id, s.company_name, s.plan,
               COALESCE(e.status, 'never received') AS intake_status,
               COALESCE(e.error, '')              AS reason
        FROM source_deals s
        LEFT JOIN target_projects t ON t.deal_id = s.deal_id
        LEFT JOIN events e          ON e.deal_id = s.deal_id
        WHERE t.deal_id IS NULL
        ORDER BY s.deal_id""",
    "Projects in Odoo with no matching closed-won deal (orphans)": """
        SELECT t.project_id, t.project_name, t.deal_id
        FROM target_projects t
        LEFT JOIN source_deals s ON s.deal_id = t.deal_id
        WHERE s.deal_id IS NULL""",
    "Duplicate projects for the same deal": """
        SELECT deal_id, COUNT(*) AS projects, GROUP_CONCAT(project_id) AS project_ids
        FROM target_projects GROUP BY deal_id HAVING COUNT(*) > 1""",
    "Customer name mismatch (CRM vs Odoo)": """
        SELECT s.deal_id, s.company_name AS crm_company, t.customer_name AS odoo_customer
        FROM source_deals s JOIN target_projects t ON t.deal_id = s.deal_id
        WHERE TRIM(LOWER(s.company_name)) <> TRIM(LOWER(COALESCE(t.customer_name, '')))""",
    "Task count differs from the plan's template": """
        SELECT s.deal_id, s.plan, s.expected_tasks, t.task_count AS actual_tasks
        FROM source_deals s JOIN target_projects t ON t.deal_id = s.deal_id
        WHERE s.expected_tasks <> t.task_count""",
}


def load_events_table(con: sqlite3.Connection, db_path: Path) -> None:
    con.execute("CREATE TABLE events (event_id TEXT, deal_id TEXT, status TEXT, error TEXT)")
    if db_path.exists():
        src = sqlite3.connect(str(db_path))
        rows = src.execute(
            # latest event per deal
            """SELECT event_id, deal_id, status, error FROM events e1
               WHERE received_at = (SELECT MAX(received_at) FROM events e2 WHERE e2.deal_id = e1.deal_id)""").fetchall()
        con.executemany("INSERT INTO events VALUES (?,?,?,?)", rows)


def md_table(cursor: sqlite3.Cursor) -> tuple[str, int]:
    rows = cursor.fetchall()
    if not rows:
        return "_None - OK_\n", 0
    cols = [d[0] for d in cursor.description]
    lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    lines += ["| " + " | ".join(str(v) for v in r) + " |" for r in rows]
    return "\n".join(lines) + "\n", len(rows)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--source", default=str(ROOT / "samples" / "deals.json"))
    args = p.parse_args()

    settings = get_settings()
    template = load_template(settings.template_path)
    odoo = build_odoo(settings)

    # ---- Source side (CRM) ----
    deals = [d for d in json.loads(Path(args.source).read_text(encoding="utf-8"))
             if d.get("stage", "closedwon") == "closedwon"]

    # ---- Target side (Odoo) ----
    projects = odoo.search_read("project.project", [("name", "ilike", "- Onboarding [")], ["name", "partner_id"])
    tasks = odoo.search_read("project.task", [("project_id", "in", [p["id"] for p in projects])], ["project_id"]) if projects else []
    task_counts: dict[int, int] = {}
    for t in tasks:
        pid = t["project_id"][0] if isinstance(t["project_id"], list) else t["project_id"]
        task_counts[pid] = task_counts.get(pid, 0) + 1

    con = sqlite3.connect(":memory:")
    con.execute("CREATE TABLE source_deals (deal_id TEXT, company_name TEXT, plan TEXT, expected_tasks INTEGER)")
    con.execute("CREATE TABLE target_projects (project_id INTEGER, project_name TEXT, deal_id TEXT, customer_name TEXT, task_count INTEGER)")
    con.executemany("INSERT INTO source_deals VALUES (?,?,?,?)", [
        (d["deal_id"], d["company_name"], str(d["plan"]).strip().title(),
         expected_task_count(template, str(d["plan"]).strip().title())) for d in deals])
    con.executemany("INSERT INTO target_projects VALUES (?,?,?,?,?)", [
        (p["id"], p["name"], deal_ref_from_project_name(p["name"]),
         p["partner_id"][1] if isinstance(p["partner_id"], list) else None,
         task_counts.get(p["id"], 0)) for p in projects])
    load_events_table(con, settings.db_path)

    synced = con.execute("SELECT COUNT(*) FROM source_deals s JOIN target_projects t USING (deal_id)").fetchone()[0]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    report = [
        "# CRM -> Odoo reconciliation report",
        f"Generated {datetime.now():%Y-%m-%d %H:%M}  |  Odoo mode: `{settings.odoo_mode}`\n",
        f"- Closed-won deals in source: **{len(deals)}**",
        f"- Onboarding projects in Odoo: **{len(projects)}**",
        f"- Matched: **{synced}** ({(synced / len(deals) * 100 if deals else 0):.0f}%)\n",
    ]
    issues = 0
    for title, sql in CHECKS.items():
        table, n = md_table(con.execute(sql))
        issues += n
        report += [f"## {title} ({n})", table]
    report.append(f"**Result: {'PASS' if issues == 0 else f'{issues} issue(s) need attention'}**")

    out = ROOT / "reports" / f"reconciliation_{stamp}.md"
    out.parent.mkdir(exist_ok=True)
    out.write_text("\n".join(report), encoding="utf-8")
    print("\n".join(report))
    print(f"\nSaved to {out.relative_to(ROOT)}")
    return 1 if issues else 0


if __name__ == "__main__":
    sys.exit(main())
