"""SQLite storage (standard library only). One file, no database server needed."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS accountants (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id                INTEGER PRIMARY KEY,
    accountant_id     INTEGER NOT NULL REFERENCES accountants(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    tax_id            TEXT,
    whatsapp_phone    TEXT UNIQUE,
    eta_client_id     TEXT,
    eta_client_secret TEXT,
    eta_profile       TEXT,   -- JSON: e-invoice issuing settings (activity code, address, item code, signer)
    notes             TEXT,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    id                INTEGER PRIMARY KEY,
    client_id         INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    direction         TEXT NOT NULL DEFAULT 'purchase',   -- purchase | sale
    status            TEXT NOT NULL,                      -- ok | review | error
    approved          INTEGER NOT NULL DEFAULT 0,
    source_type       TEXT,
    channel           TEXT NOT NULL DEFAULT 'upload',     -- upload | whatsapp | eta_sync | manual
    original_filename TEXT,
    stored_path       TEXT,
    period            TEXT NOT NULL,                      -- YYYY-MM
    supplier_name     TEXT,
    supplier_tax_id   TEXT,
    buyer_name        TEXT,
    buyer_tax_id      TEXT,
    invoice_number    TEXT,
    invoice_date      TEXT,
    currency          TEXT DEFAULT 'EGP',
    subtotal          REAL,
    vat_rate          REAL,
    vat_amount        REAL,
    total             REAL,
    eta_uuid          TEXT,
    eta_url           TEXT,
    eta_draft         TEXT,   -- JSON: receiver + lines for issuing this (sale) invoice on the ETA
    eta_state         TEXT,   -- issuing: submitted | valid | invalid | rejected
    eta_message       TEXT,   -- JSON list: ETA errors for the last submission
    eta_long_id       TEXT,
    eta_check         TEXT,   -- purchases after a sync: matched | missing (not on the ETA)
    issues_json       TEXT NOT NULL DEFAULT '[]',
    raw_text          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_client_period ON invoices(client_id, period);
CREATE INDEX IF NOT EXISTS idx_invoices_uuid ON invoices(eta_uuid);

-- Counterparties the accountant has approved before (suppliers and customers), per office.
CREATE TABLE IF NOT EXISTS parties (
    accountant_id INTEGER NOT NULL REFERENCES accountants(id) ON DELETE CASCADE,
    tax_id        TEXT NOT NULL,
    name          TEXT,
    invoice_count INTEGER NOT NULL DEFAULT 0,
    details_json  TEXT,       -- receiver type + address, remembered for e-invoices
    last_seen     TEXT NOT NULL,
    PRIMARY KEY (accountant_id, tax_id)
);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    message_id  TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    invoice_id  INTEGER,
    created_at  TEXT NOT NULL
);
"""

# Same rule as InvoiceService.summary() and the Excel export: no errors (or approved anyway), in EGP.
COUNTED = "(i.status != 'error' OR i.approved = 1) AND COALESCE(i.currency, 'EGP') = 'EGP'"

INVOICE_FIELDS = (
    "supplier_name", "supplier_tax_id", "buyer_name", "buyer_tax_id", "invoice_number",
    "invoice_date", "currency", "subtotal", "vat_rate", "vat_amount", "total",
)


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    # Columns added after the first release; older databases get them here.
    MIGRATIONS = {
        "clients": {"eta_profile": "TEXT"},
        "invoices": {"eta_draft": "TEXT", "eta_state": "TEXT", "eta_message": "TEXT",
                     "eta_long_id": "TEXT", "eta_check": "TEXT"},
        "parties": {"details_json": "TEXT"},
    }

    def _migrate(self, conn) -> None:
        for table, columns in self.MIGRATIONS.items():
            existing = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns.items():
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # norm(): Arabic-insensitive matching for search (ة/ه, أ/إ/آ/ا, ى/ي, Arabic digits, case)
        conn.create_function("norm", 1, _norm, deterministic=True)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------------------------------------------------------------- accounts
    def create_accountant(self, name: str, email: str, password: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO accountants(name, email, password_hash, created_at) VALUES (?,?,?,?)",
                (name.strip(), email.strip().lower(), hash_password(password), now()),
            )
            return cur.lastrowid

    def authenticate(self, email: str, password: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM accountants WHERE email = ?", (email.strip().lower(),)).fetchone()
        if row and verify_password(password, row["password_hash"]):
            return row
        return None

    def get_accountant(self, accountant_id: int) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM accountants WHERE id = ?", (accountant_id,)).fetchone()

    def email_exists(self, email: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM accountants WHERE email = ?",
                                (email.strip().lower(),)).fetchone() is not None

    # ----------------------------------------------------------------- clients
    def create_client(self, accountant_id: int, name: str, tax_id: str = "", whatsapp_phone: str = "",
                      notes: str = "", eta_client_id: str = "", eta_client_secret: str = "") -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO clients(accountant_id, name, tax_id, whatsapp_phone, notes, eta_client_id, "
                "eta_client_secret, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (accountant_id, name.strip(), digits(tax_id), normalize_phone(whatsapp_phone),
                 notes.strip() or None, eta_client_id.strip() or None, eta_client_secret.strip() or None, now()),
            )
            return cur.lastrowid

    def update_client(self, client_id: int, **fields) -> None:
        allowed = {"name", "tax_id", "whatsapp_phone", "notes", "eta_client_id", "eta_client_secret", "eta_profile"}
        values = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "tax_id":
                value = digits(value)
            elif key == "whatsapp_phone":
                value = normalize_phone(value)
            elif key == "eta_profile" and isinstance(value, dict):
                value = json.dumps(value, ensure_ascii=False)
            elif isinstance(value, str):
                value = value.strip() or None
            values[key] = value
        if not values:
            return
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE clients SET {sets} WHERE id = ?", (*values.values(), client_id))

    def delete_client(self, client_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))

    def get_client(self, client_id: int, accountant_id: Optional[int] = None) -> Optional[sqlite3.Row]:
        sql, args = "SELECT * FROM clients WHERE id = ?", [client_id]
        if accountant_id is not None:
            sql += " AND accountant_id = ?"
            args.append(accountant_id)
        with self.connect() as conn:
            return conn.execute(sql, args).fetchone()

    def client_by_phone(self, phone: str) -> Optional[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM clients WHERE whatsapp_phone = ?",
                                (normalize_phone(phone),)).fetchone()

    def phone_taken(self, phone: str, except_client_id: Optional[int] = None) -> bool:
        phone = normalize_phone(phone)
        if not phone:
            return False
        with self.connect() as conn:
            row = conn.execute("SELECT id FROM clients WHERE whatsapp_phone = ?", (phone,)).fetchone()
        return row is not None and row["id"] != except_client_id

    def list_clients(self, accountant_id: int, period: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT c.*,
                  COUNT(i.id) AS invoice_count,
                  SUM(CASE WHEN i.status = 'error' AND i.approved = 0 THEN 1 ELSE 0 END) AS error_count,
                  SUM(CASE WHEN i.status = 'review' AND i.approved = 0 THEN 1 ELSE 0 END) AS review_count,
                  SUM(CASE WHEN i.direction = 'purchase' AND {COUNTED}
                           THEN COALESCE(i.vat_amount, 0) ELSE 0 END) AS input_vat,
                  SUM(CASE WHEN i.direction = 'sale' AND {COUNTED}
                           THEN COALESCE(i.vat_amount, 0) ELSE 0 END) AS output_vat,
                  (SELECT MAX(created_at) FROM invoices WHERE client_id = c.id) AS last_received
                FROM clients c
                LEFT JOIN invoices i ON i.client_id = c.id AND i.period = ?
                WHERE c.accountant_id = ?
                GROUP BY c.id
                ORDER BY c.name
                """,
                (period, accountant_id),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- invoices
    def insert_invoice(self, client_id: int, data: dict) -> int:
        cols = ["client_id", "direction", "status", "approved", "source_type", "channel",
                "original_filename", "stored_path", "period", *INVOICE_FIELDS,
                "eta_uuid", "eta_url", "eta_draft", "eta_check", "issues_json", "raw_text", "created_at", "updated_at"]
        row = {
            "client_id": client_id, "approved": 0, "channel": "upload", "direction": "purchase",
            "issues_json": "[]", "created_at": now(), "updated_at": now(), **data,
        }
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO invoices({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                [row.get(c) for c in cols],
            )
            return cur.lastrowid

    def update_invoice(self, invoice_id: int, data: dict) -> None:
        allowed = {"direction", "status", "approved", "source_type", "period", "eta_uuid", "eta_url",
                   "issues_json", "raw_text", "eta_draft", "eta_state", "eta_message", "eta_long_id", "eta_check",
                   "channel", "stored_path", "original_filename", *INVOICE_FIELDS}
        values = {k: v for k, v in data.items() if k in allowed}
        values["updated_at"] = now()
        sets = ", ".join(f"{k} = ?" for k in values)
        with self.connect() as conn:
            conn.execute(f"UPDATE invoices SET {sets} WHERE id = ?", (*values.values(), invoice_id))

    def delete_invoice(self, invoice_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM invoices WHERE id = ?", (invoice_id,))

    def get_invoice(self, invoice_id: int, accountant_id: Optional[int] = None) -> Optional[dict]:
        sql = ("SELECT i.*, c.accountant_id, c.name AS client_name, c.tax_id AS client_tax_id "
               "FROM invoices i JOIN clients c ON c.id = i.client_id WHERE i.id = ?")
        args: list = [invoice_id]
        if accountant_id is not None:
            sql += " AND c.accountant_id = ?"
            args.append(accountant_id)
        with self.connect() as conn:
            row = conn.execute(sql, args).fetchone()
        return _invoice_dict(row) if row else None

    SEARCH_SORTS = {
        "date_desc": "COALESCE(i.invoice_date, i.created_at) DESC, i.id DESC",
        "date_asc": "COALESCE(i.invoice_date, i.created_at) ASC, i.id ASC",
        "total_desc": "COALESCE(i.total, 0) DESC, i.id DESC",
        "vat_desc": "COALESCE(i.vat_amount, 0) DESC, i.id DESC",
        "recent": "i.created_at DESC, i.id DESC",
    }

    def search_invoices(self, accountant_id: int, q: str = "", client_id: Optional[int] = None,
                        period: Optional[str] = None, status: Optional[str] = None,
                        direction: Optional[str] = None, channel: Optional[str] = None,
                        date_from: Optional[str] = None, date_to: Optional[str] = None,
                        amount_min: Optional[float] = None, amount_max: Optional[float] = None,
                        sort: str = "date_desc", limit: int = 100, offset: int = 0) -> tuple[list[dict], dict]:
        """Invoices across the office's clients matching the filters, plus totals for the whole result."""
        where, args = ["c.accountant_id = ?"], [accountant_id]
        q = (q or "").strip()
        if q:
            like = f"%{_norm(q)}%"
            text = ("(norm(i.supplier_name) LIKE ? OR norm(i.buyer_name) LIKE ? OR norm(i.invoice_number) LIKE ? "
                    "OR norm(i.supplier_tax_id) LIKE ? OR norm(i.buyer_tax_id) LIKE ? OR norm(i.original_filename) LIKE ? "
                    "OR norm(c.name) LIKE ?")
            text_args = [like] * 7
            amount = _as_number(q)
            if amount is not None:
                text += " OR ABS(COALESCE(i.total,0) - ?) < 0.01 OR ABS(COALESCE(i.subtotal,0) - ?) < 0.01 " \
                        "OR ABS(COALESCE(i.vat_amount,0) - ?) < 0.01"
                text_args += [amount] * 3
            where.append(text + ")")
            args += text_args
        if client_id:
            where.append("i.client_id = ?")
            args.append(client_id)
        if period:
            where.append("i.period = ?")
            args.append(period)
        status_sql = {
            "pending": "i.approved = 0 AND i.status != 'ok'",
            "error": "i.approved = 0 AND i.status = 'error'",
            "review": "i.approved = 0 AND i.status = 'review'",
            "ok": "(i.status = 'ok' OR i.approved = 1)",
            "approved": "i.approved = 1",
        }.get(status or "")
        if status_sql:
            where.append(status_sql)
        if direction in ("purchase", "sale"):
            where.append("i.direction = ?")
            args.append(direction)
        if channel in ("upload", "whatsapp", "eta_sync", "manual"):
            where.append("i.channel = ?")
            args.append(channel)
        if date_from:
            where.append("i.invoice_date >= ?")
            args.append(date_from)
        if date_to:
            where.append("i.invoice_date <= ?")
            args.append(date_to)
        if amount_min is not None:
            where.append("COALESCE(i.total, 0) >= ?")
            args.append(amount_min)
        if amount_max is not None:
            where.append("COALESCE(i.total, 0) <= ?")
            args.append(amount_max)
        base = f"FROM invoices i JOIN clients c ON c.id = i.client_id WHERE {' AND '.join(where)}"
        order = self.SEARCH_SORTS.get(sort, self.SEARCH_SORTS["date_desc"])
        with self.connect() as conn:
            rows = conn.execute(f"SELECT i.*, c.name AS client_name {base} ORDER BY {order} LIMIT ? OFFSET ?",
                                (*args, limit, offset)).fetchall()
            agg = conn.execute(
                f"SELECT COUNT(*) AS n, SUM(COALESCE(i.subtotal,0)) AS subtotal, SUM(COALESCE(i.vat_amount,0)) AS vat, "
                f"SUM(COALESCE(i.total,0)) AS total {base}", args).fetchone()
        totals = {k: (agg[k] or 0) for k in ("n", "subtotal", "vat", "total")}
        return [_invoice_dict(r) for r in rows], totals

    def search_clients(self, accountant_id: int, q: str, limit: int = 8) -> list[dict]:
        like = f"%{_norm(q)}%"
        with self.connect() as conn:
            rows = conn.execute("SELECT id, name, tax_id FROM clients WHERE accountant_id = ? AND "
                                "(norm(name) LIKE ? OR norm(tax_id) LIKE ?) ORDER BY name LIMIT ?",
                                (accountant_id, like, like, limit)).fetchall()
        return [dict(r) for r in rows]

    def list_invoices(self, client_id: int, period: Optional[str] = None,
                      status: Optional[str] = None, direction: Optional[str] = None) -> list[dict]:
        sql, args = "SELECT * FROM invoices WHERE client_id = ?", [client_id]
        if period:
            sql += " AND period = ?"
            args.append(period)
        status_filters = {
            "pending": " AND approved = 0 AND status != 'ok'",
            "error": " AND approved = 0 AND status = 'error'",
            "review": " AND approved = 0 AND status = 'review'",
            "ok": " AND (status = 'ok' OR approved = 1)",
        }
        sql += status_filters.get(status or "", "")
        if direction in ("purchase", "sale"):
            sql += " AND direction = ?"
            args.append(direction)
        sql += " ORDER BY COALESCE(invoice_date, created_at) DESC, id DESC"
        with self.connect() as conn:
            return [_invoice_dict(r) for r in conn.execute(sql, args).fetchall()]

    # ----------------------------------------------------------- party memory
    def remember_party(self, accountant_id: int, tax_id: Optional[str], name: Optional[str]) -> None:
        if not tax_id or len(tax_id) != 9:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO parties(accountant_id, tax_id, name, invoice_count, last_seen) VALUES (?,?,?,1,?)
                ON CONFLICT(accountant_id, tax_id) DO UPDATE SET
                  name = COALESCE(excluded.name, parties.name),
                  invoice_count = parties.invoice_count + 1,
                  last_seen = excluded.last_seen
                """, (accountant_id, tax_id, (name or "").strip() or None, now()))

    def party_details(self, accountant_id: int, tax_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT name, details_json FROM parties WHERE accountant_id = ? AND tax_id = ?",
                               (accountant_id, tax_id)).fetchone()
        if not row:
            return {}
        details = json.loads(row["details_json"] or "{}")
        details.setdefault("name", row["name"])
        return details

    def save_party_details(self, accountant_id: int, tax_id: str, name: Optional[str], details: dict) -> None:
        if not tax_id:
            return
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO parties(accountant_id, tax_id, name, invoice_count, last_seen, details_json)
                VALUES (?,?,?,0,?,?)
                ON CONFLICT(accountant_id, tax_id) DO UPDATE SET
                  name = COALESCE(excluded.name, parties.name), details_json = excluded.details_json
                """, (accountant_id, tax_id, (name or "").strip() or None, now(),
                      json.dumps(details, ensure_ascii=False)))

    def next_internal_id(self, client_id: int, prefix: str) -> str:
        """Next free invoice number like PREFIX-0007 among this client's sales."""
        with self.connect() as conn:
            rows = conn.execute("SELECT invoice_number FROM invoices WHERE client_id = ? AND direction = 'sale' "
                                "AND invoice_number LIKE ?", (client_id, prefix + "%")).fetchall()
        used = [int(m.group(1)) for r in rows if (m := re.fullmatch(re.escape(prefix) + r"(\d+)", r[0] or ""))]
        return f"{prefix}{(max(used) + 1 if used else 1):04d}"

    def purchases_for_matching(self, client_id: int) -> list[dict]:
        """Purchases/sales not yet linked to an ETA document (candidates to match a synced document)."""
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM invoices WHERE client_id = ? AND eta_uuid IS NULL "
                                "AND channel != 'eta_sync'", (client_id,)).fetchall()
        return [_invoice_dict(r) for r in rows]

    def known_parties(self, accountant_id: int) -> dict:
        with self.connect() as conn:
            rows = conn.execute("SELECT tax_id, name, invoice_count FROM parties WHERE accountant_id = ?",
                                (accountant_id,)).fetchall()
        return {r["tax_id"]: dict(r) for r in rows}

    def approve_many(self, client_id: int, ids: list[int]) -> list[dict]:
        """Approve the given invoices of one client, skipping any with errors. Returns the approved rows."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        with self.connect() as conn:
            rows = conn.execute(f"SELECT * FROM invoices WHERE client_id = ? AND id IN ({marks}) "
                                f"AND status != 'error' AND approved = 0", (client_id, *ids)).fetchall()
            conn.executemany("UPDATE invoices SET approved = 1, updated_at = ? WHERE id = ?",
                             [(now(), r["id"]) for r in rows])
        return [_invoice_dict(r) for r in rows]

    def work_stats(self, accountant_id: int, period: str) -> dict:
        """Invoices the system read for the accountant this month (to show the time it saved)."""
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT COUNT(*) AS total,
                  SUM(CASE WHEN i.channel = 'eta_sync' THEN 1 ELSE 0 END) AS from_eta,
                  SUM(CASE WHEN i.channel = 'whatsapp' THEN 1 ELSE 0 END) AS from_whatsapp,
                  SUM(CASE WHEN i.source_type IS NOT NULL AND i.source_type != 'unknown' THEN 1 ELSE 0 END) AS auto_read
                FROM invoices i JOIN clients c ON c.id = i.client_id
                WHERE c.accountant_id = ? AND i.period = ?
                """, (accountant_id, period)).fetchone()
        return {k: row[k] or 0 for k in row.keys()}

    # -------------------------------------------------------------- analytics
    def monthly_series(self, accountant_id: int, periods: list[str]) -> dict:
        """Per month: output/input VAT (counted invoices) and invoice counts per channel."""
        marks = ",".join("?" * len(periods))
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT i.period,
                  SUM(CASE WHEN i.direction = 'sale' AND {COUNTED} THEN COALESCE(i.vat_amount, 0) ELSE 0 END) AS output_vat,
                  SUM(CASE WHEN i.direction = 'purchase' AND {COUNTED} THEN COALESCE(i.vat_amount, 0) ELSE 0 END) AS input_vat,
                  COUNT(*) AS invoices,
                  SUM(CASE WHEN i.channel = 'upload' THEN 1 ELSE 0 END) AS upload,
                  SUM(CASE WHEN i.channel = 'whatsapp' THEN 1 ELSE 0 END) AS whatsapp,
                  SUM(CASE WHEN i.channel = 'eta_sync' THEN 1 ELSE 0 END) AS eta_sync,
                  SUM(CASE WHEN i.status = 'error' THEN 1 ELSE 0 END) AS errors,
                  SUM(CASE WHEN i.status = 'ok' AND i.source_type IS NOT NULL AND i.source_type != 'unknown'
                      THEN 1 ELSE 0 END) AS clean_auto
                FROM invoices i JOIN clients c ON c.id = i.client_id
                WHERE c.accountant_id = ? AND i.period IN ({marks})
                GROUP BY i.period
                """, (accountant_id, *periods)).fetchall()
        found = {r["period"]: dict(r) for r in rows}
        empty = {"output_vat": 0, "input_vat": 0, "invoices": 0, "upload": 0, "whatsapp": 0, "eta_sync": 0,
                 "errors": 0, "clean_auto": 0}
        return {p: {**empty, **{k: v or 0 for k, v in found.get(p, {}).items() if k != "period"}} for p in periods}

    def top_suppliers(self, accountant_id: int, period: str, limit: int = 8) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                f"""
                SELECT COALESCE(NULLIF(i.supplier_tax_id, ''), i.supplier_name) AS key,
                  MAX(i.supplier_name) AS name, MAX(i.supplier_tax_id) AS tax_id,
                  COUNT(*) AS invoices, COUNT(DISTINCT i.client_id) AS clients,
                  SUM(COALESCE(i.vat_amount, 0)) AS vat, SUM(COALESCE(i.total, 0)) AS total
                FROM invoices i JOIN clients c ON c.id = i.client_id
                WHERE c.accountant_id = ? AND i.period = ? AND i.direction = 'purchase' AND {COUNTED}
                GROUP BY key ORDER BY vat DESC LIMIT ?
                """, (accountant_id, period, limit)).fetchall()
        return [dict(r) for r in rows]

    def period_issues(self, accountant_id: int, period: str) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT i.issues_json FROM invoices i JOIN clients c ON c.id = i.client_id "
                "WHERE c.accountant_id = ? AND i.period = ?", (accountant_id, period)).fetchall()
        return [json.loads(r["issues_json"] or "[]") for r in rows]

    def pending_invoices(self, accountant_id: int, limit: int = 500) -> list[dict]:
        """Every invoice that still needs the accountant, across all clients (the review queue)."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT i.*, c.name AS client_name FROM invoices i JOIN clients c ON c.id = i.client_id
                WHERE c.accountant_id = ? AND i.approved = 0 AND i.status != 'ok'
                ORDER BY c.name, CASE i.status WHEN 'error' THEN 0 ELSE 1 END, i.id
                LIMIT ?
                """, (accountant_id, limit)).fetchall()
        return [_invoice_dict(r) for r in rows]

    def pending_count(self, accountant_id: int) -> int:
        with self.connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM invoices i JOIN clients c ON c.id = i.client_id "
                "WHERE c.accountant_id = ? AND i.approved = 0 AND i.status != 'ok'", (accountant_id,)).fetchone()[0]

    def find_duplicate(self, client_id: int, invoice: dict, exclude_id: Optional[int] = None) -> Optional[dict]:
        """Same invoice already stored for this client (by ETA UUID or supplier + number)."""
        clauses, args = [], []
        if invoice.get("eta_uuid"):
            clauses.append("eta_uuid = ?")
            args.append(invoice["eta_uuid"])
        if invoice.get("supplier_tax_id") and invoice.get("invoice_number"):
            clauses.append("(supplier_tax_id = ? AND LOWER(invoice_number) = LOWER(?))")
            args += [invoice["supplier_tax_id"], invoice["invoice_number"]]
        if not clauses:
            return None
        sql = f"SELECT * FROM invoices WHERE client_id = ? AND ({' OR '.join(clauses)})"
        params = [client_id, *args]
        if exclude_id:
            # Re-checking a stored invoice: only an *older* copy makes it a duplicate,
            # so editing the original never flags it against its own later copy.
            sql += " AND id < ?"
            params.append(exclude_id)
        with self.connect() as conn:
            row = conn.execute(sql + " ORDER BY id LIMIT 1", params).fetchone()
        return _invoice_dict(row) if row else None

    def uuid_exists(self, client_id: int, uuid: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM invoices WHERE client_id = ? AND eta_uuid = ?",
                                (client_id, uuid)).fetchone() is not None

    def periods(self, client_id: int) -> list[str]:
        with self.connect() as conn:
            rows = conn.execute("SELECT DISTINCT period FROM invoices WHERE client_id = ? ORDER BY period DESC",
                                (client_id,)).fetchall()
        return [r["period"] for r in rows]

    # ----------------------------------------------------------------- whatsapp
    def whatsapp_seen(self, message_id: str) -> bool:
        with self.connect() as conn:
            return conn.execute("SELECT 1 FROM whatsapp_messages WHERE message_id = ?",
                                (message_id,)).fetchone() is not None

    def record_whatsapp(self, message_id: str, phone: str, invoice_id: Optional[int] = None) -> bool:
        """Returns False if the message was already recorded (Meta retries webhooks)."""
        with self._lock, self.connect() as conn:
            try:
                conn.execute("INSERT INTO whatsapp_messages(message_id, phone, invoice_id, created_at) "
                             "VALUES (?,?,?,?)", (message_id, phone, invoice_id, now()))
                return True
            except sqlite3.IntegrityError:
                return False

    def link_whatsapp_invoice(self, message_id: str, invoice_id: int) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE whatsapp_messages SET invoice_id = ? WHERE message_id = ?",
                         (invoice_id, message_id))


def _norm(value) -> str:
    if value is None:
        return ""
    from invoice_extractor.normalize import normalize_text

    return normalize_text(str(value)).lower().replace("-", "").replace(" ", "")


def _as_number(text: str) -> Optional[float]:
    from invoice_extractor.normalize import normalize_text

    cleaned = normalize_text(text).replace(",", "").strip()
    try:
        return float(cleaned)
    except ValueError:
        return None


def _invoice_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["issues"] = json.loads(data.get("issues_json") or "[]")
    data["eta_errors"] = json.loads(data.get("eta_message") or "[]")
    return data


# ---------------------------------------------------------------------- helpers
def digits(value) -> Optional[str]:
    if not value:
        return None
    s = str(value).translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789"))
    s = re.sub(r"\D", "", s)
    return s or None


def normalize_phone(value) -> Optional[str]:
    """Egyptian numbers to WhatsApp's international format: 01001234567 → 201001234567."""
    s = digits(value)
    if not s:
        return None
    if s.startswith("00"):
        s = s[2:]
    if s.startswith("0") and len(s) == 11:
        s = "2" + s
    return s


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 240_000).hex()
    return f"pbkdf2_sha256$240000${salt}${digest}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
    except ValueError:
        return False
    check = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    return hmac.compare_digest(check, digest)
