"""Data models for extracted invoices."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional


@dataclass
class Issue:
    """A validation finding. level is 'error' (must fix) or 'warning' (review)."""

    level: str
    code: str
    message: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Invoice:
    source_file: str
    source_type: str = "unknown"  # eta_qr | pdf_text | ai_vision | unknown
    supplier_name: Optional[str] = None
    supplier_tax_id: Optional[str] = None
    buyer_name: Optional[str] = None
    buyer_tax_id: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None  # ISO yyyy-mm-dd
    currency: str = "EGP"
    subtotal: Optional[float] = None  # net amount before VAT
    vat_amount: Optional[float] = None
    vat_rate: Optional[float] = None  # e.g. 0.14
    total: Optional[float] = None
    eta_uuid: Optional[str] = None
    eta_url: Optional[str] = None
    raw_text: Optional[str] = None
    issues: list[Issue] = field(default_factory=list)
    status: str = "pending"  # ok | review | error

    def add_issue(self, level: str, code: str, message: str) -> None:
        self.issues.append(Issue(level, code, message))

    def to_dict(self, include_raw_text: bool = False) -> dict:
        data = asdict(self)
        data["issues"] = [i.to_dict() for i in self.issues]
        if not include_raw_text:
            data.pop("raw_text", None)
        return data

    # Fields that the extractors fill; used when merging results.
    FIELDS = (
        "supplier_name", "supplier_tax_id", "buyer_name", "buyer_tax_id",
        "invoice_number", "invoice_date", "currency", "subtotal",
        "vat_amount", "vat_rate", "total",
    )

    def merge_missing(self, other: dict) -> list[str]:
        """Fill fields that are still empty from another extraction result."""
        filled = []
        for name in self.FIELDS:
            value = other.get(name)
            if value in (None, ""):
                continue
            current = getattr(self, name)
            if current in (None, "") or (name == "currency" and current == "EGP" and value != "EGP"):
                setattr(self, name, value)
                filled.append(name)
        return filled
