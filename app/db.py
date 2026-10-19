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
    channel           TEXT NOT NULL DEFAULT 'upload',     -- upload | whatsapp | eta_sync
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
    issues_json       TEXT NOT NULL DEFAULT '[]',
    raw_text          TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_invoices_client_period ON invoices(client_id, period);
CREATE INDEX IF NOT EXISTS idx_invoices_uuid ON invoices(eta_uuid);

CREATE TABLE IF NOT EXISTS whatsapp_messages (
    message_id  TEXT PRIMARY KEY,
    phone       TEXT NOT NULL,
    invoice_id  INTEGER,
    created_at  TEXT NOT NULL
);
"""

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

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
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
        allowed = {"name", "tax_id", "whatsapp_phone", "notes", "eta_client_id", "eta_client_secret"}
        values = {}
        for key, value in fields.items():
            if key not in allowed:
                continue
            if key == "tax_id":
                value = digits(value)
            elif key == "whatsapp_phone":
                value = normalize_phone(value)
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
                """
                SELECT c.*,
                  COUNT(i.id) AS invoice_count,
                  SUM(CASE WHEN i.status = 'error' AND i.approved = 0 THEN 1 ELSE 0 END) AS error_count,
                  SUM(CASE WHEN i.status = 'review' AND i.approved = 0 THEN 1 ELSE 0 END) AS review_count,
                  SUM(CASE WHEN i.direction = 'purchase' AND (i.status != 'error' OR i.approved = 1)
                           THEN COALESCE(i.vat_amount, 0) ELSE 0 END) AS input_vat,
                  SUM(CASE WHEN i.direction = 'sale' AND (i.status != 'error' OR i.approved = 1)
                           THEN COALESCE(i.vat_amount, 0) ELSE 0 END) AS output_vat
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
                "eta_uuid", "eta_url", "issues_json", "raw_text", "created_at", "updated_at"]
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
                   "issues_json", "raw_text", *INVOICE_FIELDS}
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
            sql += " AND id != ?"
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


def _invoice_dict(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["issues"] = json.loads(data.get("issues_json") or "[]")
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
