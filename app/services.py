"""Business logic shared by the dashboard, WhatsApp and ETA sync."""
from __future__ import annotations

import json
import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Optional

from invoice_extractor import normalize
from invoice_extractor.eta_client import ETAClient, ETAError, map_document, month_range
from invoice_extractor.models import Invoice
from invoice_extractor.pipeline import SUPPORTED_EXTS, clean_fields, process_file
from invoice_extractor.validation import set_status, validate_invoice

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)


class UploadError(ValueError):
    pass


def current_period() -> str:
    return date.today().strftime("%Y-%m")


def valid_period(value: Optional[str]) -> str:
    if value and len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit():
        return value
    return current_period()


@dataclass
class SyncResult:
    imported: int = 0
    skipped: int = 0
    errors: list = None

    def __post_init__(self):
        self.errors = self.errors or []


class InvoiceService:
    def __init__(self, db: Database, settings: Settings, eta_client_factory=ETAClient):
        self.db = db
        self.settings = settings
        self.eta_client_factory = eta_client_factory

    # ------------------------------------------------------------ helpers
    def eta_client(self, client) -> Optional[ETAClient]:
        if client["eta_client_id"] and client["eta_client_secret"]:
            return self.eta_client_factory(client["eta_client_id"], client["eta_client_secret"],
                                           env=self.settings.eta_env)
        return None

    @staticmethod
    def detect_direction(client, inv: Invoice) -> str:
        tax_id = client["tax_id"]
        if tax_id and inv.supplier_tax_id == tax_id:
            return "sale"
        return "purchase"

    def _finalize(self, client, inv: Invoice, exclude_id: Optional[int] = None,
                  direction: Optional[str] = None, period: Optional[str] = None) -> dict:
        """Cross-invoice checks (duplicates in the DB) and the row to store."""
        record = {name: getattr(inv, name) for name in Invoice.FIELDS}
        record.update(eta_uuid=inv.eta_uuid, eta_url=inv.eta_url, source_type=inv.source_type)
        duplicate = self.db.find_duplicate(client["id"], record, exclude_id=exclude_id)
        if duplicate:
            inv.add_issue("error", "duplicate", f"Duplicate of invoice #{duplicate['id']}.",
                          f"الفاتورة دي متسجلة قبل كده (رقم {duplicate['id']} في النظام).")
            set_status(inv)
        record.update(
            status=inv.status,
            direction=direction if direction in ("purchase", "sale") else self.detect_direction(client, inv),
            period=valid_period(period or (inv.invoice_date or "")[:7] or None),
            issues_json=json.dumps([i.to_dict() for i in inv.issues], ensure_ascii=False),
        )
        return record

    # ------------------------------------------------------------ uploads
    def store_file(self, client_id: int, filename: str, content: bytes) -> Path:
        ext = Path(filename or "").suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise UploadError(f"نوع الملف {ext or '؟'} مش مدعوم. المدعوم: PDF وصور (JPG, PNG, WEBP).")
        if len(content) > self.settings.max_upload_mb * 1024 * 1024:
            raise UploadError(f"الملف أكبر من {self.settings.max_upload_mb} ميجا.")
        if not content:
            raise UploadError("الملف فاضي.")
        folder = self.settings.upload_dir / str(client_id) / current_period()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuidlib.uuid4().hex}{ext}"
        path.write_bytes(content)
        return path

    def add_file(self, client, filename: str, content: bytes, channel: str = "upload") -> int:
        path = self.store_file(client["id"], filename, content)
        return self.process_stored(client, path, filename, channel)

    def process_stored(self, client, path: Path, original_filename: str, channel: str = "upload",
                       existing_id: Optional[int] = None) -> int:
        eta = self.eta_client(client)
        inv = process_file(path, use_ai=self.settings.use_ai,
                           eta_fetcher=eta.fetch_fields if eta else None)
        inv.source_file = original_filename
        record = self._finalize(client, inv, exclude_id=existing_id)
        record["raw_text"] = inv.raw_text
        if existing_id:
            record["approved"] = 0
            self.db.update_invoice(existing_id, record)
            return existing_id
        record.update(channel=channel, original_filename=original_filename, stored_path=str(path))
        return self.db.insert_invoice(client["id"], record)

    def reprocess(self, client, invoice: dict) -> int:
        if not invoice.get("stored_path") or not Path(invoice["stored_path"]).exists():
            raise UploadError("الملف الأصلي مش موجود.")
        return self.process_stored(client, Path(invoice["stored_path"]), invoice["original_filename"],
                                   existing_id=invoice["id"])

    # -------------------------------------------------------------- editing
    def save_edit(self, client, invoice: dict, form: dict, approve: bool = False) -> None:
        values = {
            "supplier_name": (form.get("supplier_name") or "").strip() or None,
            "supplier_tax_id": normalize.digits_only(form.get("supplier_tax_id")),
            "buyer_name": (form.get("buyer_name") or "").strip() or None,
            "buyer_tax_id": normalize.digits_only(form.get("buyer_tax_id")),
            "invoice_number": (form.get("invoice_number") or "").strip() or None,
            "invoice_date": normalize.parse_date(form.get("invoice_date")),
            "currency": (form.get("currency") or "EGP").strip().upper(),
            "subtotal": normalize.parse_amount(form.get("subtotal")),
            "vat_amount": normalize.parse_amount(form.get("vat_amount")),
            "total": normalize.parse_amount(form.get("total")),
            "source_type": invoice.get("source_type"),
            "eta_uuid": invoice.get("eta_uuid"),
            "eta_url": invoice.get("eta_url"),
        }
        inv = validate_invoice(Invoice.from_fields(invoice.get("original_filename") or "", values))
        record = self._finalize(client, inv, exclude_id=invoice["id"],
                                direction=form.get("direction"), period=form.get("period"))
        has_errors = inv.status == "error"
        record["approved"] = 1 if approve and not has_errors else 0
        self.db.update_invoice(invoice["id"], record)

    def approve(self, invoice: dict) -> bool:
        if invoice["status"] == "error":
            return False
        self.db.update_invoice(invoice["id"], {"approved": 1})
        return True

    # ------------------------------------------------------------- ETA sync
    def sync_from_eta(self, client, period: str) -> SyncResult:
        result = SyncResult()
        eta = self.eta_client(client)
        if not eta:
            result.errors.append("مفيش بيانات دخول للمنظومة للعميل ده.")
            return result
        start, end = month_range(period)
        try:
            summaries = list(eta.search_documents(start, end))
        except (ETAError, Exception) as exc:  # network errors included
            result.errors.append(f"فشل الاتصال بالمنظومة: {exc}")
            return result
        for summary in summaries:
            doc_uuid = summary.get("uuid")
            if not doc_uuid or (summary.get("status") and summary.get("status") != "Valid"):
                result.skipped += 1
                continue
            if self.db.uuid_exists(client["id"], doc_uuid):
                result.skipped += 1
                continue
            try:
                fields = map_document(eta.get_document(doc_uuid))
            except Exception as exc:
                fields = map_document(summary)
                result.errors.append(f"{doc_uuid}: {exc}")
            fields["eta_uuid"] = fields.get("eta_uuid") or doc_uuid
            self.import_eta_fields(client, fields, period)
            result.imported += 1
        return result

    def import_eta_fields(self, client, fields: dict, period: Optional[str] = None) -> int:
        inv = Invoice(source_file=f"ETA {fields.get('eta_uuid')}")
        for name, value in clean_fields(fields).items():
            if name in Invoice.FIELDS and value not in (None, ""):
                setattr(inv, name, value)
        inv.eta_uuid = fields.get("eta_uuid")
        inv.source_type = "eta_api"
        validate_invoice(inv)
        record = self._finalize(client, inv, period=(inv.invoice_date or "")[:7] or period)
        record.update(channel="eta_sync", original_filename=inv.source_file)
        if inv.status != "error":
            record["approved"] = 1  # official record, no manual review needed
        return self.db.insert_invoice(client["id"], record)

    # -------------------------------------------------------------- summary
    def summary(self, client_id: int, period: str) -> dict:
        invoices = self.db.list_invoices(client_id, period)
        counted = [i for i in invoices if (i["status"] != "error" or i["approved"]) and i["currency"] == "EGP"]

        def block(direction: str) -> dict:
            rows = [i for i in counted if i["direction"] == direction]
            return {
                "count": len(rows),
                "subtotal": round(sum(i["subtotal"] or 0 for i in rows), 2),
                "vat": round(sum(i["vat_amount"] or 0 for i in rows), 2),
                "total": round(sum(i["total"] or 0 for i in rows), 2),
            }

        purchases, sales = block("purchase"), block("sale")
        return {
            "period": period,
            "invoice_count": len(invoices),
            "purchases": purchases,
            "sales": sales,
            "net_vat": round(sales["vat"] - purchases["vat"], 2),
            "approved_count": sum(1 for i in invoices if i["approved"] or i["status"] == "ok"),
            "review_count": sum(1 for i in invoices if i["status"] == "review" and not i["approved"]),
            "error_count": sum(1 for i in invoices if i["status"] == "error" and not i["approved"]),
            "non_egp_count": sum(1 for i in invoices if i["currency"] != "EGP"),
        }
