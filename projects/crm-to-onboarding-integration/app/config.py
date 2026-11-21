"""Settings loaded from environment variables (or a .env file)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    webhook_secret: str
    odoo_mode: str              # "mock" (no account needed) or "live"
    odoo_url: str
    odoo_db: str
    odoo_user: str
    odoo_api_key: str
    db_path: Path               # integration's own event log / ID mapping
    mock_odoo_path: Path        # where the fake Odoo keeps its records
    mock_failure_rate: float    # 0.0-1.0: simulate "429 Too Many Requests" in mock mode
    signature_tolerance: int    # seconds; older signed requests are rejected (replay protection)
    template_path: Path


def get_settings() -> Settings:
    return Settings(
        webhook_secret=os.getenv("WEBHOOK_SECRET", "change-me-local-dev-secret"),
        odoo_mode=os.getenv("ODOO_MODE", "mock").lower(),
        odoo_url=os.getenv("ODOO_URL", "http://localhost:8069"),
        odoo_db=os.getenv("ODOO_DB", "onboarding"),
        odoo_user=os.getenv("ODOO_USER", "admin"),
        odoo_api_key=os.getenv("ODOO_API_KEY", ""),
        db_path=Path(os.getenv("DB_PATH", ROOT / "data" / "integration.db")),
        mock_odoo_path=Path(os.getenv("MOCK_ODOO_PATH", ROOT / "data" / "mock_odoo.db")),
        mock_failure_rate=float(os.getenv("MOCK_FAILURE_RATE", "0")),
        signature_tolerance=int(os.getenv("SIGNATURE_TOLERANCE_SECONDS", "300")),
        template_path=Path(os.getenv("TEMPLATE_PATH", ROOT / "config" / "onboarding_template.json")),
    )
