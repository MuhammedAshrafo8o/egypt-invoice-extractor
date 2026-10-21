"""Build, check and serialize an ETA e-invoice (document type "I", version 1.0).

Follows the public ETA SDK: https://sdk.invoicing.eta.gov.eg/documents/invoice-v1-0/
and the serialization rules in "Document Serialization Approach" (keys upper-cased and
quoted, values quoted as-is, arrays repeat their name before each element).

Line math (no fees, no value difference, no after-tax item discount):
    salesTotal = quantity × unit price
    netTotal   = salesTotal − discount
    VAT (T1)   = netTotal × rate / 100
    total      = netTotal + VAT
Every amount is rounded to 5 decimals. Integral values are written as integers so the
serialized text is the same whichever JSON library re-reads the document.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

# Receiver ID becomes mandatory for a person (type P) at or above this invoice total (EGP).
PERSON_ID_THRESHOLD = 50_000

# VAT kinds offered in the editor -> (ETA subType, fixed rate or None when the user types it)
VAT_KINDS = {
    "standard": ("V009", 14.0),   # General item sales
    "other": ("V010", None),      # Other rates (the user types the rate)
    "exempt": ("V003", 0.0),      # Exempted good or service
    "export": ("V001", 0.0),      # Export
}
VAT_KIND_LABELS = {
    "standard": "١٤٪",
    "other": "نسبة تانية",
    "exempt": "معفاة",
    "export": "تصدير",
}
UNIT_TYPES = {
    "EA": "قطعة",
    "KGM": "كيلو",
    "LTR": "لتر",
    "MTR": "متر",
    "MTK": "متر مربع",
    "TNE": "طن",
    "HUR": "ساعة",
    "DAY": "يوم",
    "MON": "شهر",
}
RECEIVER_TYPES = {"B": "شركة مصرية", "P": "شخص", "F": "جهة أجنبية"}


def r5(value: float) -> float:
    return round(float(value) + 0.0, 5)


def num(value: float):
    """5-decimal number; integral values become int (1140.0 -> 1140)."""
    value = r5(value)
    return int(value) if value == int(value) else value


def clean_text(value) -> str:
    """Strip characters that make the serialized form ambiguous (double quotes) and extra spaces."""
    return re.sub(r"\s+", " ", str(value or "").replace('"', "'")).strip()


@dataclass
class Line:
    description: str
    quantity: float
    unit_price: float
    discount: float = 0.0
    vat_kind: str = "standard"
    vat_rate: Optional[float] = None  # only used for vat_kind == "other"
    unit_type: str = "EA"
    item_code: str = ""
    item_type: str = ""

    @property
    def rate(self) -> float:
        fixed = VAT_KINDS.get(self.vat_kind, VAT_KINDS["standard"])[1]
        return fixed if fixed is not None else float(self.vat_rate or 0)

    @property
    def sub_type(self) -> str:
        return VAT_KINDS.get(self.vat_kind, VAT_KINDS["standard"])[0]

    def amounts(self) -> dict:
        sales = r5(self.quantity * self.unit_price)
        net = r5(sales - (self.discount or 0))
        vat = r5(net * self.rate / 100)
        return {"sales": sales, "discount": r5(self.discount or 0), "net": net, "vat": vat, "total": r5(net + vat)}

    @classmethod
    def from_dict(cls, data: dict) -> "Line":
        def f(key, default=0.0):
            try:
                return float(data.get(key) if data.get(key) not in (None, "") else default)
            except (TypeError, ValueError):
                return default
        return cls(
            description=clean_text(data.get("description")),
            quantity=f("quantity", 1.0),
            unit_price=f("unit_price"),
            discount=f("discount"),
            vat_kind=data.get("vat_kind") if data.get("vat_kind") in VAT_KINDS else "standard",
            vat_rate=f("vat_rate", 0.0),
            unit_type=data.get("unit_type") or "EA",
            item_code=clean_text(data.get("item_code")),
            item_type=clean_text(data.get("item_type")).upper(),
        )

    def to_dict(self) -> dict:
        return {"description": self.description, "quantity": self.quantity, "unit_price": self.unit_price,
                "discount": self.discount, "vat_kind": self.vat_kind, "vat_rate": self.vat_rate,
                "unit_type": self.unit_type, "item_code": self.item_code, "item_type": self.item_type}


@dataclass
class Party:
    type: str = "B"
    id: str = ""
    name: str = ""
    governate: str = ""
    region_city: str = ""
    street: str = ""
    building_number: str = ""
    country: str = "EG"
    branch_id: str = "0"

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Party":
        data = data or {}
        return cls(**{k: clean_text(data.get(k)) or getattr(cls, k) for k in cls.__dataclass_fields__})

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}

    def address(self, issuer: bool = False) -> dict:
        addr = {}
        if issuer:
            addr["branchID"] = self.branch_id or "0"
        addr.update(country=(self.country or "EG").upper(), governate=self.governate,
                    regionCity=self.region_city, street=self.street, buildingNumber=self.building_number)
        return addr

    def has_address(self) -> bool:
        return all([self.governate, self.region_city, self.street, self.building_number])


@dataclass
class Draft:
    """Everything needed to issue one invoice; stored as JSON on the invoice row."""
    receiver: Party = field(default_factory=Party)
    lines: list[Line] = field(default_factory=list)
    internal_id: str = ""
    issue_date: str = ""  # yyyy-mm-dd

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "Draft":
        data = data or {}
        return cls(receiver=Party.from_dict(data.get("receiver")),
                   lines=[Line.from_dict(l) for l in data.get("lines") or []],
                   internal_id=clean_text(data.get("internal_id")),
                   issue_date=str(data.get("issue_date") or ""))

    def to_dict(self) -> dict:
        return {"receiver": self.receiver.to_dict(), "lines": [l.to_dict() for l in self.lines],
                "internal_id": self.internal_id, "issue_date": self.issue_date}

    def totals(self) -> dict:
        amounts = [l.amounts() for l in self.lines]
        sales = r5(sum(a["sales"] for a in amounts))
        discount = r5(sum(a["discount"] for a in amounts))
        net = r5(sum(a["net"] for a in amounts))
        vat = r5(sum(a["vat"] for a in amounts))
        return {"sales": sales, "discount": discount, "net": net, "vat": vat, "total": r5(net + vat)}


def issued_at(issue_date: str, now: Optional[datetime] = None) -> str:
    """UTC timestamp for dateTimeIssued. Today -> a few minutes ago; a past day -> 08:00 UTC."""
    now = now or datetime.now(timezone.utc)
    try:
        day = date.fromisoformat(issue_date)
    except ValueError:
        day = now.date()
    cairo_today = (now + timedelta(hours=3)).date()
    if day >= cairo_today:
        stamp = now - timedelta(minutes=5)
        return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"{day.isoformat()}T08:00:00Z"


def check_draft(draft: Draft, issuer: Party, profile: dict, today: Optional[date] = None) -> list[str]:
    """Problems that would make the ETA reject the document, in Arabic. Empty list = ready."""
    today = today or date.today()
    problems = []
    if not re.fullmatch(r"\d{9}", issuer.id or ""):
        problems.append("الرقم الضريبي للعميل (البائع) لازم يبقى ٩ أرقام — ظبطه من بيانات العميل.")
    if not issuer.name:
        problems.append("اسم العميل المسجّل في الضرائب ناقص.")
    if not (profile.get("activity_code") or "").strip():
        problems.append("كود النشاط ناقص — ظبطه من إعدادات الفاتورة الإلكترونية للعميل.")
    if not issuer.has_address():
        problems.append("عنوان العميل (المحافظة، المدينة، الشارع، رقم المبنى) ناقص في إعدادات الفاتورة الإلكترونية.")
    if not draft.internal_id:
        problems.append("رقم الفاتورة ناقص.")
    try:
        day = date.fromisoformat(draft.issue_date)
        if day > today:
            problems.append("تاريخ الفاتورة في المستقبل، والمنظومة بترفض ده.")
    except ValueError:
        problems.append("تاريخ الفاتورة ناقص أو مش مفهوم.")

    rec = draft.receiver
    total = draft.totals()["total"]
    if rec.type not in RECEIVER_TYPES:
        problems.append("نوع المشتري مش مظبوط.")
    elif rec.type == "B":
        if not re.fullmatch(r"\d{9}", rec.id or ""):
            problems.append("الرقم الضريبي للمشتري لازم يبقى ٩ أرقام.")
        elif rec.id == issuer.id:
            problems.append("المشتري والبائع ليهم نفس الرقم الضريبي.")
        if not rec.name:
            problems.append("اسم المشتري ناقص.")
        if not rec.has_address():
            problems.append("عنوان المشتري ناقص (المحافظة، المدينة، الشارع، رقم المبنى) — مطلوب للشركات.")
    elif rec.type == "P":
        if total >= PERSON_ID_THRESHOLD:
            if not re.fullmatch(r"\d{14}", rec.id or ""):
                problems.append(f"الفاتورة {total:,.0f} جنيه أو أكتر، فلازم الرقم القومي للمشتري (١٤ رقم).")
            if not rec.name or not rec.has_address():
                problems.append("اسم وعنوان المشتري مطلوبين للفواتير الكبيرة للأفراد.")
        elif rec.id and not re.fullmatch(r"\d{14}", rec.id):
            problems.append("الرقم القومي للمشتري لازم يبقى ١٤ رقم (أو سيبه فاضي).")
    elif rec.type == "F":
        if not rec.id or not rec.name:
            problems.append("للجهة الأجنبية: الرقم الضريبي في بلدها والاسم مطلوبين.")
        if not rec.country or rec.country.upper() == "EG" or len(rec.country) != 2:
            problems.append("اكتب كود دولة المشتري الأجنبي (حرفين، مثلًا SA أو AE).")

    if not draft.lines:
        problems.append("لازم صنف واحد على الأقل.")
    default_code = (profile.get("item_code") or "").strip()
    for n, line in enumerate(draft.lines, 1):
        label = f"الصنف {n}"
        if not line.description:
            problems.append(f"{label}: الوصف ناقص.")
        if line.quantity <= 0:
            problems.append(f"{label}: الكمية لازم تبقى أكبر من صفر.")
        if line.unit_price < 0:
            problems.append(f"{label}: السعر مينفعش يبقى بالسالب.")
        if line.discount < 0 or line.discount > line.quantity * line.unit_price:
            problems.append(f"{label}: الخصم أكبر من قيمة الصنف.")
        if not (line.item_code or default_code):
            problems.append(f"{label}: كود الصنف (EGS أو GS1) ناقص — سجّل كود في إعدادات العميل أو اكتبه للصنف.")
        if line.vat_kind == "other" and not (0 < (line.vat_rate or 0) <= 100):
            problems.append(f"{label}: اكتب نسبة الضريبة.")
    return problems


def build_document(draft: Draft, issuer: Party, profile: dict, now: Optional[datetime] = None) -> dict:
    """The ETA invoice document (without signatures)."""
    default_type = (profile.get("item_type") or "EGS").upper()
    default_code = (profile.get("item_code") or "").strip()
    default_unit = profile.get("unit_type") or "EA"
    lines, vat_total = [], 0.0
    for line in draft.lines:
        a = line.amounts()
        vat_total += a["vat"]
        lines.append({
            "description": line.description,
            "itemType": line.item_type or default_type,
            "itemCode": line.item_code or default_code,
            "unitType": line.unit_type or default_unit,
            "quantity": num(line.quantity),
            "internalCode": "",
            "salesTotal": num(a["sales"]),
            "total": num(a["total"]),
            "valueDifference": 0,
            "totalTaxableFees": 0,
            "netTotal": num(a["net"]),
            "itemsDiscount": 0,
            "unitValue": {"currencySold": "EGP", "amountEGP": num(line.unit_price)},
            "discount": {"rate": 0, "amount": num(a["discount"])},
            "taxableItems": [{"taxType": "T1", "amount": num(a["vat"]), "subType": line.sub_type,
                              "rate": num(line.rate)}],
        })
    totals = draft.totals()
    rec = draft.receiver
    receiver = {"type": rec.type, "id": rec.id, "name": rec.name}
    if rec.type != "P" or rec.has_address():
        receiver["address"] = rec.address()
    return {
        "issuer": {"type": "B", "id": issuer.id, "name": issuer.name, "address": issuer.address(issuer=True)},
        "receiver": receiver,
        "documentType": "I",
        "documentTypeVersion": "1.0",
        "dateTimeIssued": issued_at(draft.issue_date, now),
        "taxpayerActivityCode": (profile.get("activity_code") or "").strip(),
        "internalID": draft.internal_id,
        "invoiceLines": lines,
        "totalDiscountAmount": num(totals["discount"]),
        "totalSalesAmount": num(totals["sales"]),
        "netAmount": num(totals["net"]),
        "taxTotals": [{"taxType": "T1", "amount": num(r5(vat_total))}],
        "totalAmount": num(totals["total"]),
        "extraDiscountAmount": 0,
        "totalItemsDiscountAmount": 0,
    }


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return json.dumps(value)
    return str(value)


def serialize(value, name: str = "") -> str:
    """ETA canonical serialization of a document (signatures must already be removed)."""
    if isinstance(value, dict):
        out = []
        for key, child in value.items():
            if isinstance(child, list):
                out.append(f'"{key.upper()}"')
                for item in child:
                    out.append(f'"{key.upper()}"' + serialize(item, key))
            else:
                out.append(f'"{key.upper()}"' + serialize(child, key))
        return "".join(out)
    if isinstance(value, list):  # top-level list (not used for invoices)
        return "".join(f'"{name.upper()}"' + serialize(v, name) for v in value)
    return f'"{_scalar(value)}"'


def document_hash_input(document: dict) -> bytes:
    doc = {k: v for k, v in document.items() if k != "signatures"}
    return serialize(doc).encode("utf-8")


def public_url(env: str, uuid: str, long_id: str) -> Optional[str]:
    if not uuid or not long_id:
        return None
    host = {"prod": "invoicing.eta.gov.eg", "preprod": "preprod.invoicing.eta.gov.eg"}.get(env)
    return f"https://{host}/print/documents/{uuid}/share/{long_id}" if host else None


def _num(value) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(str(value).replace(",", "").translate(str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")))
    except ValueError:
        return None


def _rate_percent(value) -> Optional[float]:
    rate = _num(value)
    if rate is None:
        return None
    return rate * 100 if rate <= 1 else rate


def lines_from_extracted(items: list, prices_include_vat: bool = False, invoice_vat_rate=None,
                         subtotal=None, total=None) -> list[Line]:
    """Item rows read off a paper/photo invoice -> editable e-invoice lines.

    Without item rows, one line carries the invoice's amount before VAT so the totals still match."""
    default_rate = _rate_percent(invoice_vat_rate)
    lines = []
    for item in items or []:
        qty = _num(item.get("quantity"))
        price = _num(item.get("unit_price"))
        amount = _num(item.get("amount"))
        if not qty:
            qty = round(amount / price, 5) if amount and price else 1.0
        if price is None and amount is not None:
            price = amount / qty
        if price is None:
            continue
        rate = _rate_percent(item.get("vat_rate"))
        rate = rate if rate is not None else (default_rate if default_rate is not None else 14.0)
        if prices_include_vat and rate:
            price = price / (1 + rate / 100)
        line = Line(description=clean_text(item.get("description")) or "صنف", quantity=qty,
                    unit_price=r5(price), discount=r5(_num(item.get("discount")) or 0))
        if abs(rate - 14) < 0.01:
            line.vat_kind = "standard"
        elif rate == 0:
            line.vat_kind = "exempt"
        else:
            line.vat_kind, line.vat_rate = "other", round(rate, 2)
        lines.append(line)
    if not lines:
        base = _num(subtotal)
        if base is None and _num(total) is not None:
            base = _num(total) / (1 + (default_rate if default_rate is not None else 14.0) / 100)
        line = Line(description="", quantity=1, unit_price=r5(base or 0))
        if default_rate is not None and abs(default_rate - 14) >= 0.01:
            line.vat_kind, line.vat_rate = ("exempt", None) if default_rate == 0 else ("other", round(default_rate, 2))
        lines.append(line)
    return lines
