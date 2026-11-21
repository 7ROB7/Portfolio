"""Odoo external API client (XML-RPC) with retries, plus a local mock with the same interface.

Odoo's external API:  https://www.odoo.com/documentation/17.0/developer/reference/external_api.html
    common.authenticate(db, login, api_key, {})          -> uid
    object.execute_kw(db, uid, api_key, model, method, args, kwargs)

Retry policy (the part interviewers ask about):
  * Only TRANSIENT failures are retried: HTTP 429/502/503/504, timeouts, dropped connections.
  * BUSINESS errors (bad field, access denied, validation) are NOT retried; retrying cannot fix them.
  * Exponential backoff with jitter (0.5s, 1s, 2s, 4s + random) so many clients don't retry in lockstep.
  * If the server sends Retry-After, honour it.
"""
from __future__ import annotations

import json
import logging
import random
import socket
import sqlite3
import threading
import time
import xmlrpc.client
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger("odoo")

RETRYABLE_HTTP = {429, 502, 503, 504}


class TransientError(Exception):
    def __init__(self, message: str, retry_after: float | None = None):
        super().__init__(message)
        self.retry_after = retry_after


class OdooError(Exception):
    """Non-retryable error (auth, validation, access rights, unknown field...)."""


class BaseOdoo:
    def __init__(self, max_retries: int = 4, base_delay: float = 0.5, sleep: Callable[[float], None] = time.sleep):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep = sleep
        self.retry_count = 0  # exposed for tests / metrics

    # --- to be implemented by subclasses -------------------------------------------------
    def _execute(self, model: str, method: str, args: list, kwargs: dict) -> Any:
        raise NotImplementedError

    # --- shared retry wrapper -------------------------------------------------------------
    def call(self, model: str, method: str, *args, **kwargs) -> Any:
        attempt = 0
        while True:
            try:
                return self._execute(model, method, list(args), kwargs)
            except TransientError as exc:
                attempt += 1
                if attempt > self.max_retries:
                    log.error("Giving up on %s.%s after %d retries: %s", model, method, self.max_retries, exc)
                    raise
                delay = exc.retry_after if exc.retry_after is not None else (
                    self.base_delay * 2 ** (attempt - 1) + random.uniform(0, self.base_delay))
                self.retry_count += 1
                log.warning("Transient error on %s.%s (%s). Retry %d/%d in %.2fs",
                            model, method, exc, attempt, self.max_retries, delay)
                self.sleep(delay)

    # --- convenience wrappers ---------------------------------------------------------------
    def search_read(self, model: str, domain: list, fields: list[str], limit: int = 0) -> list[dict]:
        return self.call(model, "search_read", domain, fields=fields, limit=limit)

    def create(self, model: str, values: dict) -> int:
        result = self.call(model, "create", values)
        return result[0] if isinstance(result, list) else result

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        return self.call(model, "write", ids, values)


# =========================================================================================
# Live Odoo (Docker Community edition or an Odoo Online trial both work)
# =========================================================================================
class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout: float, **kw):
        super().__init__(**kw)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class _TimeoutSafeTransport(xmlrpc.client.SafeTransport):
    def __init__(self, timeout: float, **kw):
        super().__init__(**kw)
        self.timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self.timeout
        return conn


class LiveOdoo(BaseOdoo):
    def __init__(self, url: str, db: str, user: str, api_key: str, timeout: float = 30, **kw):
        super().__init__(**kw)
        if not api_key:
            raise OdooError("ODOO_API_KEY is empty. Create one in Odoo: Preferences > Account Security > New API Key")
        self.url, self.db, self.user, self.api_key = url.rstrip("/"), db, user, api_key
        transport_cls = _TimeoutSafeTransport if self.url.startswith("https") else _TimeoutTransport
        self._common = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/common", transport=transport_cls(timeout), allow_none=True)
        self._models = xmlrpc.client.ServerProxy(f"{self.url}/xmlrpc/2/object", transport=transport_cls(timeout), allow_none=True)
        self._uid: int | None = None

    def _wrap(self, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except xmlrpc.client.ProtocolError as exc:
            if exc.errcode in RETRYABLE_HTTP:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                raise TransientError(f"HTTP {exc.errcode}", float(retry_after) if retry_after else None) from exc
            raise OdooError(f"HTTP {exc.errcode}: {exc.errmsg}") from exc
        except xmlrpc.client.Fault as exc:
            raise OdooError(exc.faultString.strip().splitlines()[-1]) from exc
        except (socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise TransientError(f"network: {exc}") from exc

    @property
    def uid(self) -> int:
        if self._uid is None:
            uid = self._wrap(lambda: self._common.authenticate(self.db, self.user, self.api_key, {}))
            if not uid:
                raise OdooError("Authentication failed: check ODOO_DB, ODOO_USER and ODOO_API_KEY")
            self._uid = uid
        return self._uid

    def _execute(self, model, method, args, kwargs):
        uid = self.uid
        return self._wrap(lambda: self._models.execute_kw(self.db, uid, self.api_key, model, method, args, kwargs))

    def version(self) -> dict:
        return self._wrap(lambda: self._common.version())


# =========================================================================================
# Mock Odoo: same interface, stores records in a local SQLite file. No account needed.
# =========================================================================================
M2O_TARGETS = {
    "partner_id": "res.partner",
    "parent_id": "res.partner",
    "project_id": "project.project",
    "stage_id": "project.task.type",
}


class MockOdoo(BaseOdoo):
    """Imitates the parts of Odoo we use: search_read / create / write, many2one display
    values like [id, "name"], many2many commands like [(4, id)], and optional random 429s."""

    def __init__(self, path: Path, failure_rate: float = 0.0, **kw):
        super().__init__(**kw)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS records (id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT, data TEXT)")
        self._db.commit()
        self._lock = threading.Lock()
        self.failure_rate = failure_rate
        self.fail_next = 0  # tests: force the next N calls to fail with a transient error

    # -- helpers --
    def _rows(self, model: str) -> list[dict]:
        cur = self._db.execute("SELECT id, data FROM records WHERE model = ?", (model,))
        return [{"id": rid, **json.loads(data)} for rid, data in cur.fetchall()]

    def _name_of(self, model: str, rid: int) -> str:
        row = self._db.execute("SELECT data FROM records WHERE model = ? AND id = ?", (model, rid)).fetchone()
        return json.loads(row[0]).get("name", "") if row else ""

    @staticmethod
    def _match(record: dict, domain: list) -> bool:
        for field, op, value in domain:
            actual = record.get(field, False)  # Odoo returns False for empty fields
            if isinstance(actual, list) and field in M2O_TARGETS:
                actual = actual[0]
            if op == "=" and actual != value:
                return False
            if op == "!=" and actual == value:
                return False
            if op == "in" and actual not in value:
                return False
            if op == "ilike" and str(value).lower() not in str(actual or "").lower():
                return False
        return True

    @staticmethod
    def _apply_m2m(existing: list | None, commands: list) -> list:
        ids = list(existing or [])
        for cmd in commands:
            if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == 4 and cmd[1] not in ids:
                ids.append(cmd[1])
            elif isinstance(cmd, int) and cmd not in ids:
                ids.append(cmd)
        return ids

    def _normalise(self, values: dict, current: dict | None = None) -> dict:
        out = dict(current or {})
        for k, v in values.items():
            if isinstance(v, list) and v and isinstance(v[0], (list, tuple)):
                out[k] = self._apply_m2m(out.get(k), v)
            else:
                out[k] = v
        return out

    # -- the fake server --
    def _execute(self, model, method, args, kwargs):
        if self.fail_next > 0:
            self.fail_next -= 1
            raise TransientError("Simulated HTTP 429 Too Many Requests", retry_after=0.05)
        if self.failure_rate and random.random() < self.failure_rate:
            raise TransientError("Simulated HTTP 429 Too Many Requests", retry_after=0.2)

        with self._lock:
            if method == "create":
                values = self._normalise(args[0])
                cur = self._db.execute("INSERT INTO records (model, data) VALUES (?, ?)", (model, json.dumps(values, default=str)))
                self._db.commit()
                return cur.lastrowid

            if method == "write":
                ids, values = args
                for rid in ids:
                    row = self._db.execute("SELECT data FROM records WHERE model=? AND id=?", (model, rid)).fetchone()
                    if not row:
                        raise OdooError(f"Record {model}({rid}) does not exist")
                    merged = self._normalise(values, json.loads(row[0]))
                    self._db.execute("UPDATE records SET data=? WHERE id=?", (json.dumps(merged, default=str), rid))
                self._db.commit()
                return True

            if method == "search_read":
                domain = args[0] if args else []
                fields = kwargs.get("fields") or []
                limit = kwargs.get("limit") or 0
                result = []
                for rec in self._rows(model):
                    if not self._match(rec, domain):
                        continue
                    shaped = {"id": rec["id"]}
                    for f in fields:
                        val = rec.get(f, False)
                        if f in M2O_TARGETS and isinstance(val, int):
                            val = [val, self._name_of(M2O_TARGETS[f], val)]  # Odoo returns [id, display_name]
                        shaped[f] = val
                    result.append(shaped)
                    if limit and len(result) >= limit:
                        break
                return result

            raise OdooError(f"MockOdoo does not implement {model}.{method}")


def build_odoo(settings) -> BaseOdoo:
    if settings.odoo_mode == "live":
        return LiveOdoo(settings.odoo_url, settings.odoo_db, settings.odoo_user, settings.odoo_api_key)
    return MockOdoo(settings.mock_odoo_path, failure_rate=settings.mock_failure_rate)
