"""Validation layer: the checks an accountant would do by eye, done automatically.

Every finding is attached to the invoice as an Issue (English + Arabic message).
Invoices end up as:
  ok     – nothing to review
  review – warnings only; a human should glance at it
  error  – a required field is missing or the numbers don't add up
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from .models import Invoice

STANDARD_VAT_RATE = 0.14
# Other rates that legitimately appear on Egyptian invoices (exempt / zero-rated, 5% on some services).
KNOWN_VAT_RATES = (0.0, 0.05, 0.14)

FIELD_NAMES_AR = {
    "supplier_tax_id": "الرقم الضريبي للمورد",
    "buyer_tax_id": "الرقم الضريبي للعميل",
    "invoice_date": "تاريخ الفاتورة",
    "total": "الإجمالي",
    "subtotal": "المبلغ قبل الضريبة",
    "vat_amount": "قيمة الضريبة",
    "invoice_number": "رقم الفاتورة",
    "supplier_name": "اسم المورد",
}


@dataclass
class ValidationConfig:
    amount_tolerance: float = 1.0       # EGP rounding tolerance
    rate_tolerance: float = 0.005       # 0.5 percentage points
    max_age_days: int = 180             # flag invoices older than this
    required_fields: tuple = ("supplier_tax_id", "invoice_date", "total")
    known_vat_rates: tuple = KNOWN_VAT_RATES
    today: date = field(default_factory=date.today)


def _check_tax_id(inv: Invoice, attr: str) -> None:
    value = getattr(inv, attr)
    if not value:
        return
    label_ar = FIELD_NAMES_AR[attr]
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    setattr(inv, attr, digits)
    if len(digits) == 9:
        return
    if len(digits) == 14:
        inv.add_issue("warning", f"{attr}_national_id",
                      f"{attr}: 14 digits — looks like a national ID, not a tax registration number.",
                      f"{label_ar} ١٤ رقم — غالبًا رقم قومي مش رقم تسجيل ضريبي.")
    else:
        inv.add_issue("error", f"{attr}_format",
                      f"{attr} '{digits}' should be 9 digits (got {len(digits)}).",
                      f"{label_ar} '{digits}' لازم يكون ٩ أرقام (الموجود {len(digits)}).")


def _check_amounts(inv: Invoice, cfg: ValidationConfig) -> None:
    sub, vat, total = inv.subtotal, inv.vat_amount, inv.total

    for name in ("subtotal", "vat_amount", "total"):
        value = getattr(inv, name)
        if value is not None and value < 0:
            inv.add_issue("warning", f"{name}_negative", f"{name} is negative — credit note?",
                          f"{FIELD_NAMES_AR[name]} بالسالب — إشعار دائن؟")

    if sub is not None and vat is not None and sub > 0:
        rate = round(vat / sub, 4)
        if inv.vat_rate is None:
            inv.vat_rate = rate
        elif abs(inv.vat_rate - rate) > cfg.rate_tolerance:
            inv.add_issue("warning", "vat_rate_mismatch",
                          f"Printed VAT rate {inv.vat_rate:.0%} but amounts imply {rate:.2%}.",
                          f"النسبة المكتوبة {inv.vat_rate:.0%} لكن المبالغ بتقول {rate:.2%}.")
        if not any(abs(rate - r) <= cfg.rate_tolerance for r in cfg.known_vat_rates):
            inv.add_issue("warning", "vat_rate_unusual",
                          f"VAT is {rate:.2%} of the subtotal — expected {STANDARD_VAT_RATE:.0%}.",
                          f"الضريبة {rate:.2%} من المبلغ — المتوقع {STANDARD_VAT_RATE:.0%}. "
                          "ممكن يكون فيه ضريبة جدول أو خصم أو رقم اتقرا غلط.")

    if sub is not None and vat is not None and total is not None:
        diff = round(total - (sub + vat), 2)
        if abs(diff) > cfg.amount_tolerance:
            inv.add_issue("error", "total_mismatch",
                          f"Total {total:,.2f} ≠ subtotal {sub:,.2f} + VAT {vat:,.2f} (off by {diff:,.2f}).",
                          f"الإجمالي {total:,.2f} ≠ المبلغ {sub:,.2f} + الضريبة {vat:,.2f} (الفرق {diff:,.2f}).")
    elif total is not None and vat is None and sub is None:
        inv.add_issue("warning", "vat_missing", "Only a total was found — VAT amount could not be read.",
                      "لقينا الإجمالي بس — قيمة الضريبة ماتقرتش.")
    elif total is not None and vat is not None and sub is None:
        inv.subtotal = round(total - vat, 2)
        inv.add_issue("warning", "subtotal_derived", "Subtotal was not printed; derived as total − VAT.",
                      "المبلغ قبل الضريبة مش مكتوب؛ اتحسب = الإجمالي − الضريبة.")


def _check_date(inv: Invoice, cfg: ValidationConfig) -> None:
    if not inv.invoice_date:
        return
    try:
        d = datetime.strptime(inv.invoice_date, "%Y-%m-%d").date()
    except ValueError:
        inv.add_issue("error", "date_format", f"Unreadable date '{inv.invoice_date}'.",
                      f"تاريخ مش مفهوم '{inv.invoice_date}'.")
        return
    if d > cfg.today + timedelta(days=1):
        inv.add_issue("error", "date_future", f"Invoice date {d} is in the future — likely day/month swapped.",
                      f"التاريخ {d} في المستقبل — غالبًا اليوم والشهر متبدلين.")
    elif (cfg.today - d).days > cfg.max_age_days:
        days = (cfg.today - d).days
        inv.add_issue("warning", "date_old", f"Invoice is {days} days old — check it belongs to this period.",
                      f"الفاتورة عمرها {days} يوم — اتأكد إنها تبع الفترة دي.")


def validate_invoice(inv: Invoice, cfg: ValidationConfig | None = None) -> Invoice:
    cfg = cfg or ValidationConfig()

    for name in cfg.required_fields:
        if getattr(inv, name) in (None, ""):
            inv.add_issue("error", f"{name}_missing", f"Required field '{name}' was not found.",
                          f"{FIELD_NAMES_AR.get(name, name)} مش موجود.")

    _check_tax_id(inv, "supplier_tax_id")
    _check_tax_id(inv, "buyer_tax_id")
    if inv.supplier_tax_id and inv.supplier_tax_id == inv.buyer_tax_id:
        inv.add_issue("warning", "same_tax_id", "Supplier and buyer have the same tax ID.",
                      "المورد والعميل ليهم نفس الرقم الضريبي.")
    if not inv.invoice_number:
        inv.add_issue("warning", "invoice_number_missing", "No invoice number — duplicates are harder to catch.",
                      "مفيش رقم فاتورة — اكتشاف التكرار هيبقى أصعب.")

    _check_amounts(inv, cfg)
    _check_date(inv, cfg)
    set_status(inv)
    return inv


def set_status(inv: Invoice) -> None:
    levels = {i.level for i in inv.issues}
    inv.status = "error" if "error" in levels else "review" if "warning" in levels else "ok"


def duplicate_key(inv: Invoice):
    if inv.eta_uuid:
        return ("uuid", inv.eta_uuid)
    if inv.supplier_tax_id and inv.invoice_number:
        return ("num", inv.supplier_tax_id, inv.invoice_number.lower())
    if inv.supplier_tax_id and inv.invoice_date and inv.total is not None:
        return ("amt", inv.supplier_tax_id, inv.invoice_date, round(inv.total, 2))
    return None


def flag_duplicates(invoices: list[Invoice]) -> None:
    """Mark invoices that appear more than once in a batch."""
    seen: dict[tuple, str] = {}
    for inv in invoices:
        key = duplicate_key(inv)
        if key is None:
            continue
        if key in seen:
            inv.add_issue("error", "duplicate", f"Same invoice as {seen[key]}.",
                          f"نفس الفاتورة دي موجودة قبل كده ({seen[key]}).")
            set_status(inv)
        else:
            seen[key] = inv.source_file
