"""Rule-based field extraction from invoice text (Arabic + English labels).

Works on text that came out of a digital PDF. Labels are written in their
*normalized* form (see normalize.normalize_text): ة→ه, أ/إ/آ→ا, ى→ي.
"""
from __future__ import annotations

import re
from typing import Optional

from ..normalize import normalize_text, parse_amount, parse_date

_SEP = r"\s*[:：#\-–]?\s*"

SUPPLIER_LABELS = r"(?:اسم المورد|اسم البائع|اسم الشركه|المورد|البائع|seller|supplier|vendor|issuer|from|company)"
BUYER_LABELS = r"(?:اسم المشتري|اسم العميل|المشتري|العميل|السيد|buyer|customer|bill(?:ed)? to|client|receiver)"
TAX_ID_LABELS = (
    r"(?:رقم التسجيل الضريبي|الرقم الضريبي|رقم ضريبي|التسجيل الضريبي|البطاقه الضريبيه|"
    r"tax\s*(?:registration|reg\.?)?\s*(?:no\.?|number|id|#)|trn|vat\s*(?:no\.?|number|id)|registration\s*no\.?)"
)
INVOICE_NO_LABELS = (
    r"(?:رقم الفاتوره|فاتوره رقم|رقم المستند|invoice\s*(?:no\.?|number|#|id)|inv\.?\s*(?:no\.?|#)|document\s*(?:no\.?|number))"
)
DATE_LABELS = r"(?:تاريخ الفاتوره|تاريخ الاصدار|تاريخ|التاريخ|invoice\s*date|date\s*of\s*issue|issue\s*date|date)"

# Money labels, checked in this priority order so "Total before VAT" never counts as the total.
SUBTOTAL_LABELS = (
    r"(?:الاجمالي قبل الضريبه|المبلغ قبل الضريبه|صافي المبلغ|صافي القيمه|المجموع الفرعي|الصافي|"
    r"sub\s*-?\s*total|net\s*(?:amount|total|value)|total\s*before\s*(?:tax|vat)|amount\s*before\s*(?:tax|vat)|total\s*excl\.?\s*vat)"
)
VAT_LABELS = r"(?:ضريبه القيمه المضافه|قيمه الضريبه|الضريبه|\bvat\b|value\s*added\s*tax|tax\s*amount)"
GRAND_TOTAL_LABELS = (
    r"(?:الاجمالي شامل الضريبه|الاجمالي المستحق|المطلوب سداده|grand\s*total|amount\s*due|"
    r"total\s*(?:due|payable|incl\.?\s*(?:vat|tax)|including\s*(?:vat|tax)))"
)
TOTAL_LABELS = (
    r"(?:الاجمالي شامل الضريبه|الاجمالي المستحق|اجمالي الفاتوره|المطلوب سداده|الاجمالي|الاجمالى|"
    r"grand\s*total|total\s*(?:amount|due|payable|incl\.?\s*(?:vat|tax)|including\s*(?:vat|tax))|amount\s*due|total)"
)

_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_PERCENT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TAX_ID = re.compile(r"(?<!\d)(\d{3}[\s\-]?\d{3}[\s\-]?\d{3}|\d{14})(?!\d)")


def _has(label: str, line: str) -> bool:
    return re.search(label, line, re.IGNORECASE) is not None


def _value_after(label: str, line: str) -> Optional[str]:
    m = re.search(label + r"\s*[:：#\-–]\s*(.+)", line, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # RTL extraction sometimes yields "value : label"
    m = re.search(r"(.+?)\s*[:：]\s*" + label + r"\s*$", line, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _last_amount(line: str) -> Optional[float]:
    cleaned = _PERCENT.sub(" ", line)
    cleaned = re.sub(r"\b\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}\b", " ", cleaned)  # drop dates
    numbers = _NUMBER.findall(cleaned)
    return parse_amount(numbers[-1]) if numbers else None


def _detect_currency(text: str) -> str:
    t = text.lower()
    if re.search(r"\busd\b|\$|دولار", t):
        return "USD"
    if re.search(r"\beur\b|€|يورو", t):
        return "EUR"
    if re.search(r"\bsar\b|ريال", t):
        return "SAR"
    return "EGP"


def _original_spelling(value: str, raw_text: str) -> str:
    """Values are matched on normalized text (ة→ه …); return them as written in the invoice."""
    for raw_line in raw_text.splitlines():
        raw_line = re.sub(r"\s+", " ", raw_line).strip()
        chars, index = [], []
        for i, ch in enumerate(raw_line):
            for c in normalize_text(ch):
                chars.append(c)
                index.append(i)
        pos = "".join(chars).find(value)
        if pos >= 0 and value:
            return raw_line[index[pos]: index[pos + len(value) - 1] + 1]
    return value


def parse_text(raw_text: str) -> dict:
    text = normalize_text(raw_text)
    lines = [re.sub(r"\s+", " ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]

    result: dict = {"currency": _detect_currency(text)}
    tax_ids: list[tuple[str, str]] = []  # (role, id)
    totals: list[float] = []

    for line in lines:
        # --- tax registration numbers -----------------------------------
        if _has(TAX_ID_LABELS, line):
            for m in _TAX_ID.finditer(line):
                tid = re.sub(r"\D", "", m.group(1))
                role = "buyer" if _has(BUYER_LABELS, line) else "unknown"
                tax_ids.append((role, tid))

        # --- names ---------------------------------------------------------
        if "supplier_name" not in result and _has(SUPPLIER_LABELS, line) and not _has(TAX_ID_LABELS, line):
            value = _value_after(SUPPLIER_LABELS, line)
            if value:
                result["supplier_name"] = value
        if "buyer_name" not in result and _has(BUYER_LABELS, line) and not _has(TAX_ID_LABELS, line):
            value = _value_after(BUYER_LABELS, line)
            if value:
                result["buyer_name"] = value

        # --- invoice number --------------------------------------------------
        if "invoice_number" not in result and _has(INVOICE_NO_LABELS, line):
            value = _value_after(INVOICE_NO_LABELS, line)
            if value:
                token = re.match(r"[A-Za-z0-9][A-Za-z0-9\-/_.]*", value)
                if token:
                    result["invoice_number"] = token.group(0).rstrip(".")

        # --- date ------------------------------------------------------------
        if "invoice_date" not in result and _has(DATE_LABELS, line):
            parsed = parse_date(line)
            if parsed:
                result["invoice_date"] = parsed

        # --- money (one field per line, by priority) -------------------------
        if _has(SUBTOTAL_LABELS, line):
            amount = _last_amount(line)
            if amount is not None:
                result.setdefault("subtotal", amount)
        elif _has(GRAND_TOTAL_LABELS, line):
            amount = _last_amount(line)
            if amount is not None:
                totals.append(amount)
        elif _has(VAT_LABELS, line) and not _has(TAX_ID_LABELS, line):
            pct = _PERCENT.search(line)
            if pct:
                result.setdefault("vat_rate", round(float(pct.group(1)) / 100, 4))
            amount = _last_amount(line)
            if amount is not None and "vat_amount" not in result:
                result["vat_amount"] = amount
        elif _has(TOTAL_LABELS, line):
            amount = _last_amount(line)
            if amount is not None:
                totals.append(amount)

    if totals:
        # The grand total is normally the last (and largest) "total" line.
        result["total"] = max(totals[-2:]) if len(totals) > 1 else totals[0]

    # Assign tax IDs: explicit buyer first, otherwise first = supplier, second = buyer.
    buyer_ids = [t for r, t in tax_ids if r == "buyer"]
    other_ids = [t for r, t in tax_ids if r != "buyer"]
    if other_ids:
        result["supplier_tax_id"] = other_ids[0]
    if buyer_ids:
        result["buyer_tax_id"] = buyer_ids[0]
    elif len(other_ids) > 1:
        result["buyer_tax_id"] = other_ids[1]

    # Supplier name fallback: first line that looks like a heading.
    if "supplier_name" not in result:
        for line in lines[:5]:
            if re.search(r"[A-Za-z؀-ۿ]{3,}", line) and not re.search(
                r"فاتوره|invoice|tax|ضريبي|:|\d{3,}", line, re.IGNORECASE
            ):
                result["supplier_name"] = line
                break

    for key in ("supplier_name", "buyer_name"):
        if result.get(key):
            result[key] = _original_spelling(result[key], raw_text)
    return result
