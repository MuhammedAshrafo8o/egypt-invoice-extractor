"""Business logic shared by the dashboard, WhatsApp and ETA sync."""
from __future__ import annotations

import json
import logging
import uuid as uuidlib
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

from invoice_extractor import normalize
from invoice_extractor.eta_client import (ETAClient, ETAError, error_messages, map_document, month_range,
                                          validation_messages)
from invoice_extractor.eta_demo import DemoETAClient
from invoice_extractor.eta_issue import Draft, Line, Party, build_document, check_draft, document_hash_input, \
    lines_from_extracted, public_url
from invoice_extractor.eta_signer import (PfxSigner, Pkcs11Signer, SigningError, check_certificate,
                                          make_test_certificate, sign_document_bytes)
from invoice_extractor.models import Invoice
from invoice_extractor.pipeline import SUPPORTED_EXTS, clean_fields, process_file
from invoice_extractor.validation import set_status, validate_invoice

from .config import Settings
from .db import Database

log = logging.getLogger(__name__)


ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


class IssueError(ValueError):
    """Problems issuing an e-invoice; .messages are Arabic lines to show."""

    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


class UploadError(ValueError):
    pass


def current_period() -> str:
    return date.today().strftime("%Y-%m")


def period_for(invoice_date: Optional[str]) -> str:
    """Month an invoice counts in: its own month, unless the date looks wrong
    (future, or more than ~13 months old — usually a misread year), then this month."""
    try:
        d = date.fromisoformat(invoice_date or "")
    except ValueError:
        return current_period()
    today = date.today()
    if (d - today).days > 1 or (today - d).days > 400:
        return current_period()
    return d.strftime("%Y-%m")


def filing_deadline(deadline_day: int = 0, today: Optional[date] = None) -> dict:
    """The VAT return being prepared now: last month's, due in this month
    (on `deadline_day`, or the month's last day when 0). Once that date passes,
    the next one (this month's return) is shown."""
    import calendar

    today = today or date.today()

    def due_in(year: int, month: int) -> date:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, min(deadline_day, last) if deadline_day else last)

    period_month = date(today.year, today.month, 1) - timedelta(days=1)  # last day of previous month
    due = due_in(today.year, today.month)
    if today > due:
        period_month = today
        nxt = (date(today.year, today.month, 28) + timedelta(days=4)).replace(day=1)
        due = due_in(nxt.year, nxt.month)
    return {"period": period_month.strftime("%Y-%m"), "due": due, "days_left": (due - today).days}


def near_miss(a: str, b: str) -> bool:
    """True when two IDs differ by one digit or by two swapped neighbouring digits (typical misreads)."""
    if not a or not b or len(a) != len(b) or a == b:
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    if len(diff) == 1:
        return True
    return (len(diff) == 2 and diff[1] == diff[0] + 1
            and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])


MINUTES_PER_INVOICE = 1.5  # typing one invoice by hand into Excel, as accountants estimate it


def wa_link(phone: Optional[str], text: str) -> Optional[str]:
    """Opens the accountant's own WhatsApp with the message ready to send (no API needed)."""
    from urllib.parse import quote

    return f"https://wa.me/{phone}?text={quote(text)}" if phone else None


def reminder_text(client_name: str, office: str, period_label: str, days_left: Optional[int] = None,
                  intake_number: str = "") -> str:
    lines = [f"أهلًا {client_name}،",
             f"محتاجين فواتير المشتريات والمبيعات بتاعة {period_label} عشان نجهز الإقرار."]
    if days_left is not None and days_left >= 0:
        lines.append(f"فاضل {days_left} يوم على ميعاد الإقرار.")
    lines.append(f"صوّر الفواتير وابعتها على الرقم {intake_number}." if intake_number
                 else "صوّر الفواتير وابعتهالي هنا.")
    lines.append(f"شكرًا، {office}")
    return "\n".join(lines)


def invoice_question_text(client_name: str, office: str, invoice: dict, problem: str) -> str:
    what = invoice.get("supplier_name") or invoice.get("original_filename") or "الفاتورة"
    when = f" بتاريخ {invoice['invoice_date']}" if invoice.get("invoice_date") else ""
    return (f"أهلًا {client_name}،\nبخصوص فاتورة {what}{when}:\n{problem}\n"
            f"ممكن تبعتلي صورة أوضح أو تأكدلي البيانات؟\nشكرًا، {office}")


def valid_period(value: Optional[str]) -> str:
    if value and len(value) == 7 and value[4] == "-" and value[:4].isdigit() and value[5:].isdigit():
        return value
    return current_period()


@dataclass
class SyncResult:
    imported: int = 0
    skipped: int = 0
    errors: list = None
    matched: int = 0      # uploaded invoices linked to their ETA document
    missing: int = 0      # purchases of the month with no ETA document
    missing_vat: float = 0.0

    def __post_init__(self):
        self.errors = self.errors or []


class InvoiceService:
    def __init__(self, db: Database, settings: Settings, eta_client_factory=ETAClient):
        self.db = db
        self.settings = settings
        self.eta_client_factory = eta_client_factory

    # ------------------------------------------------------------ helpers
    def eta_client(self, client) -> Optional[ETAClient]:
        if self.settings.eta_demo:
            return DemoETAClient(store=self.demo_store, taxpayer=client["tax_id"] or "")
        if client["eta_client_id"] and client["eta_client_secret"]:
            return self.eta_client_factory(client["eta_client_id"], client["eta_client_secret"],
                                           env=self.settings.eta_env)
        return None

    @property
    def demo_store(self) -> Path:
        return Path(self.settings.data_dir or self.settings.database_path.parent) / "eta_demo.json"

    @staticmethod
    def detect_direction(client, inv: Invoice) -> str:
        tax_id = client["tax_id"]
        if tax_id and inv.supplier_tax_id == tax_id:
            return "sale"
        return "purchase"

    def _finalize(self, client, inv: Invoice, exclude_id: Optional[int] = None,
                  direction: Optional[str] = None, period: Optional[str] = None) -> dict:
        """Cross-invoice checks (duplicates in the DB) and the row to store."""
        self._apply_party_memory(client, inv)
        record = {name: getattr(inv, name) for name in Invoice.FIELDS}
        record.update(eta_uuid=inv.eta_uuid, eta_url=inv.eta_url, source_type=inv.source_type)
        if inv.eta_uuid:
            record["eta_check"] = "matched"
        duplicate = self.db.find_duplicate(client["id"], record, exclude_id=exclude_id)
        if duplicate:
            inv.add_issue("error", "duplicate", f"Duplicate of invoice #{duplicate['id']}.",
                          f"الفاتورة دي متسجلة قبل كده (رقم {duplicate['id']} في النظام).")
            set_status(inv)
        record.update(
            status=inv.status,
            direction=direction if direction in ("purchase", "sale") else self.detect_direction(client, inv),
            period=valid_period(period) if period else period_for(inv.invoice_date),
            issues_json=json.dumps([i.to_dict() for i in inv.issues], ensure_ascii=False),
        )
        return record

    # ------------------------------------------------------- party memory
    def _apply_party_memory(self, client, inv: Invoice) -> None:
        """Fill names of known suppliers/customers and flag tax IDs one digit away from a known one."""
        known = self.db.known_parties(client["accountant_id"])
        if not known:
            return
        for id_attr, name_attr, label in (("supplier_tax_id", "supplier_name", "المورد"),
                                          ("buyer_tax_id", "buyer_name", "المشتري")):
            tax_id = getattr(inv, id_attr)
            if not tax_id or tax_id == client["tax_id"]:
                continue
            if tax_id in known:
                if not getattr(inv, name_attr) and known[tax_id]["name"]:
                    setattr(inv, name_attr, known[tax_id]["name"])
                continue
            close = [p for t, p in known.items() if near_miss(tax_id, t)]
            if close:
                best = max(close, key=lambda p: p["invoice_count"])
                inv.add_issue(
                    "warning", f"{id_attr}_near_known",
                    f"{label} tax ID {tax_id} is one digit away from known party {best['name']} ({best['tax_id']}).",
                    f"الرقم الضريبي لـ{label} {tax_id} قريب جدًا من رقم {best['name'] or 'طرف معروف'} "
                    f"({best['tax_id']}) اللي اتعامل معاه قبل كده — غالبًا رقم اتقرا غلط.")
                set_status(inv)

    def remember_parties(self, client, invoice: dict) -> None:
        for tax_attr, name_attr in (("supplier_tax_id", "supplier_name"), ("buyer_tax_id", "buyer_name")):
            tax_id = invoice.get(tax_attr)
            if tax_id and tax_id != client["tax_id"]:
                self.db.remember_party(client["accountant_id"], tax_id, invoice.get(name_attr))

    def approve_many(self, client, ids: list[int]) -> int:
        approved = self.db.approve_many(client["id"], ids)
        for invoice in approved:
            self.remember_parties(client, invoice)
        return len(approved)

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
        eta = None if self.settings.eta_demo else self.eta_client(client)
        inv = process_file(path, use_ai=self.settings.use_ai, use_ocr=self.settings.use_ocr,
                           eta_fetcher=eta.fetch_fields if eta else None)
        inv.source_file = original_filename
        record = self._finalize(client, inv, exclude_id=existing_id)
        record["raw_text"] = inv.raw_text
        if existing_id:
            record["approved"] = 0
            self.db.update_invoice(existing_id, record)
            return existing_id
        record.update(channel=channel, original_filename=original_filename, stored_path=str(path))
        if record["direction"] == "sale" and inv.line_items:
            # Item rows were read: keep them so issuing the e-invoice starts pre-filled.
            record["eta_draft"] = json.dumps(self.draft_from_extracted(client, inv).to_dict(), ensure_ascii=False)
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
        if record["approved"]:
            self.remember_parties(client, record)

    def approve(self, invoice: dict) -> bool:
        if invoice["status"] == "error":
            return False
        self.db.update_invoice(invoice["id"], {"approved": 1})
        self.remember_parties(self.db.get_client(invoice["client_id"]), invoice)
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
            match = self.find_upload_match(client, fields)
            if match:
                self.merge_eta_into(client, match, fields)
                result.matched += 1
            else:
                self.import_eta_fields(client, fields, period)
                result.imported += 1
        self.flag_missing_purchases(client, period, result)
        return result

    # ------------------------------------------------- matching with the ETA
    @staticmethod
    def _same_number(a: Optional[str], b: Optional[str]) -> bool:
        def key(v):
            return "".join(ch for ch in str(v or "").translate(ARABIC_DIGITS).lower() if ch.isalnum()).lstrip("0")
        return bool(key(a)) and key(a) == key(b)

    def find_upload_match(self, client, fields: dict) -> Optional[dict]:
        """An uploaded invoice that is the paper/PDF copy of this ETA document."""
        supplier, buyer = fields.get("supplier_tax_id"), fields.get("buyer_tax_id")
        total = fields.get("total")
        try:
            eta_day = date.fromisoformat(fields.get("invoice_date") or "")
        except ValueError:
            eta_day = None
        best = None
        for inv in self.db.purchases_for_matching(client["id"]):
            if not supplier or inv.get("supplier_tax_id") != supplier:
                continue
            if any(x.get("code") == "duplicate" for x in inv["issues"]):
                continue
            if buyer and inv.get("buyer_tax_id") and inv["buyer_tax_id"] != buyer:
                continue
            if self._same_number(inv.get("invoice_number"), fields.get("invoice_number")):
                return inv
            try:
                close_day = eta_day and abs((date.fromisoformat(inv.get("invoice_date") or "") - eta_day).days) <= 3
            except ValueError:
                close_day = False
            if close_day and total is not None and inv.get("total") is not None and abs(inv["total"] - total) <= 1:
                best = best or inv
        return best

    def merge_eta_into(self, client, invoice: dict, fields: dict) -> None:
        """The ETA copy is the official one: take its numbers, keep the uploaded file."""
        values = {k: invoice.get(k) for k in Invoice.FIELDS}
        for name, value in clean_fields(fields).items():
            if name in Invoice.FIELDS and value not in (None, ""):
                values[name] = value
        values.update(eta_uuid=fields.get("eta_uuid"), source_type=invoice.get("source_type"))
        inv = validate_invoice(Invoice.from_fields(invoice.get("original_filename") or "", values))
        record = self._finalize(client, inv, exclude_id=invoice["id"], direction=invoice["direction"])
        record.update(eta_check="matched", approved=1 if inv.status != "error" else 0)
        self.db.update_invoice(invoice["id"], record)
        if record["approved"]:
            self.remember_parties(client, record)

    def flag_missing_purchases(self, client, period: str, result: Optional[SyncResult] = None) -> None:
        """After a successful sync: purchases of the month with no ETA document are flagged."""
        for inv in self.db.list_invoices(client["id"], period):
            if inv["direction"] != "purchase" or inv["channel"] == "eta_sync":
                continue
            if any(x.get("code") == "duplicate" for x in inv["issues"]):
                continue  # a second copy of another invoice: not a separate missing document
            state = "matched" if inv.get("eta_uuid") else "missing"
            if inv.get("eta_check") != state:
                self.db.update_invoice(inv["id"], {"eta_check": state})
            if state == "missing" and result is not None:
                result.missing += 1
                if inv["currency"] == "EGP" and (inv["status"] != "error" or inv["approved"]):
                    result.missing_vat = round(result.missing_vat + (inv["vat_amount"] or 0), 2)

    def missing_on_eta(self, client_id: int, period: str) -> list[dict]:
        return [i for i in self.db.list_invoices(client_id, period)
                if i["direction"] == "purchase" and i.get("eta_check") == "missing"]

    def import_eta_fields(self, client, fields: dict, period: Optional[str] = None) -> int:
        inv = Invoice(source_file=f"ETA {fields.get('eta_uuid')}")
        for name, value in clean_fields(fields).items():
            if name in Invoice.FIELDS and value not in (None, ""):
                setattr(inv, name, value)
        inv.eta_uuid = fields.get("eta_uuid")
        inv.source_type = "eta_api"
        validate_invoice(inv)
        record = self._finalize(client, inv)
        record.update(channel="eta_sync", original_filename=inv.source_file)
        if inv.status != "error":
            record["approved"] = 1  # official record, no manual review needed
            self.remember_parties(client, record)
        return self.db.insert_invoice(client["id"], record)

    # ------------------------------------------------- issuing e-invoices
    PROFILE_KEYS = ("issuer_name", "activity_code", "branch_id", "governate", "region_city", "street",
                    "building_number", "item_type", "item_code", "unit_type", "signer", "pfx_path", "number_prefix")

    @staticmethod
    def profile(client) -> dict:
        try:
            data = json.loads(client["eta_profile"] or "{}")
        except (TypeError, ValueError, KeyError, IndexError):
            data = {}
        data.setdefault("item_type", "EGS")
        data.setdefault("unit_type", "EA")
        data.setdefault("branch_id", "0")
        data.setdefault("signer", "token")
        data.setdefault("number_prefix", "INV-")
        return data

    def save_profile(self, client, form: dict) -> None:
        data = {k: (form.get(k) or "").strip() for k in self.PROFILE_KEYS}
        data["item_type"] = data["item_type"].upper() if data["item_type"].upper() in ("EGS", "GS1") else "EGS"
        data["signer"] = data["signer"] if data["signer"] in ("token", "pfx") else "token"
        self.db.update_client(client["id"], eta_profile=data)

    def issuer(self, client) -> Party:
        p = self.profile(client)
        return Party(type="B", id=client["tax_id"] or "", name=p.get("issuer_name") or client["name"],
                     governate=p.get("governate", ""), region_city=p.get("region_city", ""),
                     street=p.get("street", ""), building_number=p.get("building_number", ""),
                     branch_id=p.get("branch_id") or "0")

    def can_issue(self, client) -> bool:
        """The ETA connection exists (or demo mode). Profile gaps are reported by check_draft."""
        return self.settings.eta_demo or bool(client["eta_client_id"] and client["eta_client_secret"])

    def new_sale(self, client) -> int:
        today = date.today().isoformat()
        prefix = self.profile(client).get("number_prefix") or "INV-"
        draft = Draft(internal_id=self.db.next_internal_id(client["id"], prefix), issue_date=today,
                      lines=[Line(description="", quantity=1, unit_price=0)])
        return self.db.insert_invoice(client["id"], {
            "direction": "sale", "status": "review", "channel": "manual", "source_type": "manual",
            "original_filename": "فاتورة مبيعات جديدة", "period": period_for(today),
            "supplier_name": self.issuer(client).name, "supplier_tax_id": client["tax_id"],
            "invoice_number": draft.internal_id, "invoice_date": today,
            "eta_draft": json.dumps(draft.to_dict(), ensure_ascii=False),
        })

    def draft_for(self, client, invoice: dict) -> Draft:
        if invoice.get("eta_draft"):
            return Draft.from_dict(json.loads(invoice["eta_draft"]))
        # First time: start from what was read off the paper invoice.
        receiver = Party(type="B", id=invoice.get("buyer_tax_id") or "", name=invoice.get("buyer_name") or "")
        if receiver.id:
            known = self.db.party_details(client["accountant_id"], receiver.id)
            if known:
                receiver = Party.from_dict({**known, "id": receiver.id, "name": receiver.name or known.get("name")})
        elif not receiver.name:
            receiver.type = "P"
        sub, vat = invoice.get("subtotal"), invoice.get("vat_amount")
        line = Line(description="", quantity=1, unit_price=float(sub or 0))
        if sub and vat is not None:
            rate = round(100 * vat / sub, 2) if sub else 14
            if abs(rate - 14) < 0.2:
                line.vat_kind = "standard"
            elif abs(rate) < 0.01:
                line.vat_kind = "exempt"
            else:
                line.vat_kind, line.vat_rate = "other", rate
        return Draft(receiver=receiver, lines=[line], internal_id=invoice.get("invoice_number") or "",
                     issue_date=invoice.get("invoice_date") or date.today().isoformat())

    def draft_from_extracted(self, client, inv: Invoice, base: Optional[Draft] = None) -> Draft:
        """A draft e-invoice from what was read off a paper/photo sales invoice."""
        base = base or Draft()
        tax_id = inv.buyer_tax_id or ""
        rtype = "B" if len(tax_id) == 9 else "P"
        receiver = Party(type=rtype, id=tax_id, name=inv.buyer_name or "")
        known = self.db.party_details(client["accountant_id"], tax_id) if rtype == "B" and tax_id else {}
        if known:
            receiver = Party.from_dict({**known, "type": "B", "id": tax_id, "name": receiver.name or known.get("name")})
        elif inv.buyer_address and rtype == "B":
            receiver.street = inv.buyer_address  # the accountant splits it into governorate/city if needed
        lines = lines_from_extracted(inv.line_items, inv.prices_include_vat, inv.vat_rate, inv.subtotal, inv.total)
        issue_date = inv.invoice_date if inv.invoice_date and inv.invoice_date <= date.today().isoformat() \
            else (base.issue_date or date.today().isoformat())
        return Draft(receiver=receiver, lines=lines, internal_id=inv.invoice_number or base.internal_id,
                     issue_date=issue_date)

    def fill_sale_from_upload(self, client, invoice: dict, filename: str, content: bytes) -> list[str]:
        """Read a paper/photo sales invoice into this (not yet issued) e-invoice. Returns Arabic notes."""
        if invoice.get("eta_state") in ("submitted", "valid"):
            raise IssueError(["الفاتورة اتبعتت للمنظومة خلاص ومينفعش تتغير."])
        try:
            path = self.store_file(client["id"], filename, content)
        except UploadError as exc:
            raise IssueError([str(exc)])
        inv = process_file(path, use_ai=self.settings.use_ai, use_ocr=self.settings.use_ocr)
        current = self.draft_for(client, invoice)
        draft = self.draft_from_extracted(client, inv, base=current)
        self.db.update_invoice(invoice["id"], {"stored_path": str(path), "original_filename": filename,
                                               "raw_text": inv.raw_text, "source_type": inv.source_type})
        self.save_draft(client, self.db.get_invoice(invoice["id"]), draft)
        notes = []
        read_issue = next((i for i in inv.issues if i.code in ("ai_failed", "ai_unavailable", "ai_disabled",
                                                             "ocr_failed")), None)
        if read_issue:
            notes.append(read_issue.message_ar)
        if not inv.line_items:
            notes.append("الأصناف ماتقرتش من الصورة، فحطيت سطر واحد بالمبلغ قبل الضريبة — اكتب الأصناف بالتفصيل.")
        totals = draft.totals()
        if inv.total and abs(totals["total"] - inv.total) > 1:
            notes.append(f"مجموع الأصناف ({totals['total']:,.2f}) مختلف عن إجمالي الفاتورة الورق "
                         f"({inv.total:,.2f}) — راجع الكميات والأسعار.")
        if client["tax_id"] and inv.buyer_tax_id == client["tax_id"]:
            notes.append(f"المشتري في الفاتورة دي هو {client['name']} نفسه — غالبًا دي فاتورة مشتريات مش مبيعات. "
                         "فاتورة المشتريات بيصدرها المورد، ارفعها في صفحة العميل بدل هنا.")
        if inv.buyer_tax_id and len(inv.buyer_tax_id) not in (9, 14):
            notes.append(f"الرقم الضريبي للمشتري اتقرا {inv.buyer_tax_id} — مش ٩ أرقام، راجعه.")
        return notes

    @staticmethod
    def draft_from_form(form) -> Draft:
        """Form fields: receiver_* and repeated line fields (description, quantity, ...)."""
        get_list = form.getlist if hasattr(form, "getlist") else (lambda k: form.get(k) or [])
        receiver = Party.from_dict({
            "type": form.get("receiver_type") or "B",
            "id": normalize.digits_only(form.get("receiver_id")) if (form.get("receiver_type") or "B") != "F"
            else (form.get("receiver_id") or ""),
            "name": form.get("receiver_name"), "governate": form.get("receiver_governate"),
            "region_city": form.get("receiver_region_city"), "street": form.get("receiver_street"),
            "building_number": form.get("receiver_building_number"),
            "country": form.get("receiver_country") or "EG",
        })
        columns = ("description", "quantity", "unit_price", "discount", "vat_kind", "vat_rate", "unit_type",
                   "item_code")
        values = {c: list(get_list(c)) for c in columns}
        lines = []
        for n in range(len(values["description"])):
            row = {c: (values[c][n] if n < len(values[c]) else "") for c in columns}
            for c in ("quantity", "unit_price", "discount", "vat_rate"):
                row[c] = normalize.parse_amount(row[c]) if row[c] not in ("", None) else None
            if not (row["description"] or "").strip() and not row["unit_price"]:
                continue  # empty row
            lines.append(Line.from_dict(row))
        return Draft(receiver=receiver, lines=lines, internal_id=(form.get("internal_id") or "").strip(),
                     issue_date=normalize.parse_date(form.get("issue_date")) or "")

    def save_draft(self, client, invoice: dict, draft: Draft) -> None:
        if invoice.get("eta_state") in ("submitted", "valid"):
            raise IssueError(["الفاتورة اتبعتت للمنظومة خلاص ومينفعش تتعدل. لو فيها غلط اعمل لها إلغاء من البوابة."])
        totals = draft.totals()
        issuer = self.issuer(client)
        values = {
            "supplier_name": issuer.name, "supplier_tax_id": client["tax_id"],
            "buyer_name": draft.receiver.name or None, "buyer_tax_id": draft.receiver.id or None,
            "invoice_number": draft.internal_id or None, "invoice_date": draft.issue_date or None,
            "currency": "EGP", "subtotal": round(totals["net"], 2), "vat_amount": round(totals["vat"], 2),
            "total": round(totals["total"], 2), "source_type": invoice.get("source_type"),
        }
        if totals["net"]:
            values["vat_rate"] = round(totals["vat"] / totals["net"], 4)
        inv = validate_invoice(Invoice.from_fields(invoice.get("original_filename") or "", values))
        if draft.receiver.type in ("P", "F"):
            # A person (national ID or none) or a foreign buyer has no Egyptian 9-digit tax ID.
            inv.issues = [i for i in inv.issues if "buyer_tax_id" not in i.code]
            set_status(inv)
        record = self._finalize(client, inv, exclude_id=invoice["id"], direction="sale",
                                period=period_for(draft.issue_date))
        record.update(eta_draft=json.dumps(draft.to_dict(), ensure_ascii=False), eta_state=None, eta_message=None)
        self.db.update_invoice(invoice["id"], record)
        rec = draft.receiver
        if rec.id and rec.type == "B":
            self.db.save_party_details(client["accountant_id"], rec.id, rec.name,
                                       {k: v for k, v in rec.to_dict().items() if k not in ("id", "name")})

    def signer_for(self, client, secret: str):
        profile = self.profile(client)
        if self.settings.eta_demo and not (profile.get("signer") == "pfx" and profile.get("pfx_path")):
            path = Path(self.settings.data_dir or self.settings.database_path.parent) / "demo-certs" / \
                f"{client['tax_id']}.pfx"
            if not path.exists():
                make_test_certificate(path, client["tax_id"] or "000000000", client["name"])
            return PfxSigner(path, "")
        if profile.get("signer") == "pfx":
            return PfxSigner(profile.get("pfx_path") or "", secret)
        return Pkcs11Signer(self.settings.eta_pkcs11_lib, secret, client["tax_id"] or "")

    def issue(self, client, invoice: dict, secret: str) -> dict:
        """Sign and submit one sale invoice. Returns the updated invoice row. Raises IssueError."""
        if invoice["direction"] != "sale":
            raise IssueError(["الفاتورة دي مشتريات. الفاتورة الإلكترونية للمشتريات بيطلعها المورد مش احنا."])
        if invoice.get("eta_state") in ("submitted", "valid"):
            raise IssueError(["الفاتورة دي اتبعتت قبل كده."])
        eta = self.eta_client(client)
        if not eta:
            raise IssueError(["العميل مش مربوط بالمنظومة — دخّل Client ID و Client Secret من بيانات العميل."])
        draft = self.draft_for(client, invoice)
        profile = self.profile(client)
        problems = check_draft(draft, self.issuer(client), profile)
        if problems:
            raise IssueError(problems)
        try:
            signer = self.signer_for(client, secret)
            cert_problems = check_certificate(signer.certificate(), client["tax_id"] or "")
            if cert_problems and not self.settings.eta_demo:
                raise IssueError(cert_problems)
            document = build_document(draft, self.issuer(client), profile)
            document["signatures"] = [{"signatureType": "I",
                                       "value": sign_document_bytes(document_hash_input(document), signer)}]
        except SigningError as exc:
            raise IssueError([str(exc)])
        try:
            response = eta.submit_documents([document])
        except Exception as exc:
            raise IssueError([f"ماقدرناش نوصل للمنظومة: {exc}"])
        accepted = response.get("acceptedDocuments") or []
        if accepted:
            doc = accepted[0]
            self.db.update_invoice(invoice["id"], {
                "eta_uuid": doc.get("uuid"), "eta_long_id": doc.get("longId"), "eta_state": "submitted",
                "eta_message": None, "eta_url": public_url(self.settings.eta_env, doc.get("uuid"), doc.get("longId"))})
            return self.refresh_issue(client, self.db.get_invoice(invoice["id"]))
        rejected = response.get("rejectedDocuments") or [{}]
        messages = error_messages(rejected[0].get("error")) or ["المنظومة رفضت الفاتورة من غير سبب واضح."]
        self.db.update_invoice(invoice["id"], {"eta_state": "rejected",
                                               "eta_message": json.dumps(messages, ensure_ascii=False)})
        return self.db.get_invoice(invoice["id"])

    def refresh_issue(self, client, invoice: dict) -> dict:
        """Ask the ETA whether a submitted invoice became Valid or Invalid."""
        if not invoice.get("eta_uuid") or invoice.get("eta_state") not in ("submitted", "valid", "invalid"):
            return invoice
        eta = self.eta_client(client)
        try:
            details = eta.get_document_details(invoice["eta_uuid"])
        except Exception as exc:
            log.warning("ETA details failed: %s", exc)
            return invoice
        status = str(details.get("status") or "").lower()
        update = {}
        if status == "valid":
            update = {"eta_state": "valid", "eta_message": None, "approved": 1, "eta_check": "matched"}
        elif status in ("invalid", "rejected", "cancelled"):
            messages = validation_messages(details) or [f"حالة الفاتورة على المنظومة: {details.get('status')}"]
            update = {"eta_state": "invalid", "eta_message": json.dumps(messages, ensure_ascii=False),
                      "eta_uuid": None if status == "invalid" else invoice["eta_uuid"]}
        long_id = details.get("longId") or invoice.get("eta_long_id")
        if long_id and status == "valid":
            update.update(eta_long_id=long_id, eta_url=public_url(self.settings.eta_env, invoice["eta_uuid"], long_id))
        if update:
            self.db.update_invoice(invoice["id"], update)
        return self.db.get_invoice(invoice["id"])

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
            "missing_on_eta": {
                "count": sum(1 for i in counted if i["direction"] == "purchase" and i.get("eta_check") == "missing"),
                "vat": round(sum(i["vat_amount"] or 0 for i in counted
                                 if i["direction"] == "purchase" and i.get("eta_check") == "missing"), 2),
            },
        }
