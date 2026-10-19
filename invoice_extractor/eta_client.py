"""Client for the Egyptian Tax Authority (ETA) e-invoicing API.

Each taxpayer (the accountant's client) creates an ERP system on the ETA portal and
gets a client_id / client_secret. With those we can:
  * get a token            POST {identity}/connect/token  (client_credentials)
  * search documents       GET  {api}/api/v1.0/documents/search
  * get one document       GET  {api}/api/v1.0/documents/{uuid}/raw

NOTE: endpoints and field names follow the public ETA SDK documentation. They have not
been tested against live credentials in this repo yet — test on the pre-production
environment first (ETA_ENV=preprod).
"""
from __future__ import annotations

import json
import time
from datetime import date, datetime, timedelta
from typing import Iterator, Optional

import requests

ENVIRONMENTS = {
    "preprod": {
        "identity": "https://id.preprod.eta.gov.eg",
        "api": "https://api.preprod.invoicing.eta.gov.eg",
    },
    "prod": {
        "identity": "https://id.eta.gov.eg",
        "api": "https://api.invoicing.eta.gov.eg",
    },
}
VAT_TAX_TYPE = "T1"  # Value-added tax in the ETA tax-type code list


class ETAError(RuntimeError):
    pass


class ETAClient:
    def __init__(self, client_id: str, client_secret: str, env: str = "preprod",
                 session: Optional[requests.Session] = None, timeout: int = 30):
        if env not in ENVIRONMENTS:
            raise ValueError(f"ETA env must be one of {list(ENVIRONMENTS)}")
        self.client_id = client_id
        self.client_secret = client_secret
        self.urls = ENVIRONMENTS[env]
        self.session = session or requests.Session()
        self.timeout = timeout
        self._token: Optional[str] = None
        self._token_expiry = 0.0

    # ------------------------------------------------------------------ auth
    def token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        response = self.session.post(
            f"{self.urls['identity']}/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "scope": "InvoicingAPI",
            },
            timeout=self.timeout,
        )
        if response.status_code != 200:
            raise ETAError(f"Login failed ({response.status_code}): {response.text[:300]}")
        payload = response.json()
        self._token = payload["access_token"]
        self._token_expiry = time.time() + int(payload.get("expires_in", 3600))
        return self._token

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        response = self.session.get(
            f"{self.urls['api']}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self.token()}", "Accept-Language": "ar"},
            timeout=self.timeout,
        )
        if response.status_code == 404:
            raise ETAError("Document not found (or this taxpayer is not its issuer/receiver).")
        if response.status_code != 200:
            raise ETAError(f"ETA API error {response.status_code}: {response.text[:300]}")
        return response.json()

    # ------------------------------------------------------------- documents
    def get_document(self, uuid: str) -> dict:
        return self._get(f"/api/v1.0/documents/{uuid}/raw")

    def search_documents(self, date_from: date, date_to: date, direction: Optional[str] = None,
                         page_size: int = 100) -> Iterator[dict]:
        """Yield document summaries submitted between two dates (direction: 'Sent' | 'Received')."""
        params = {
            "submissionDateFrom": f"{date_from.isoformat()}T00:00:00Z",
            "submissionDateTo": f"{date_to.isoformat()}T23:59:59Z",
            "pageSize": page_size,
        }
        if direction:
            params["direction"] = direction
        while True:
            payload = self._get("/api/v1.0/documents/search", params)
            yield from payload.get("result", [])
            token = (payload.get("metadata") or {}).get("continuationToken")
            if not token or token == "EndofResultSet":
                break
            params["continuationToken"] = token

    def fetch_fields(self, uuid: str) -> dict:
        """Convenience for invoice_extractor.process_file(eta_fetcher=...)."""
        return map_document(self.get_document(uuid))


def month_range(period: str) -> tuple[date, date]:
    start = datetime.strptime(period + "-01", "%Y-%m-%d").date()
    end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    return start, end


def _first(*values):
    for value in values:
        if value not in (None, ""):
            return value
    return None


def map_document(raw: dict) -> dict:
    """Map an ETA document (raw endpoint or search summary) to invoice fields."""
    doc = raw.get("document") if isinstance(raw.get("document"), (dict, str)) else None
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except json.JSONDecodeError:
            doc = None
    doc = doc or {}
    issuer = doc.get("issuer") or {}
    receiver = doc.get("receiver") or {}

    vat = None
    for tax in doc.get("taxTotals") or []:
        if str(tax.get("taxType", "")).upper() == VAT_TAX_TYPE:
            vat = (vat or 0) + float(tax.get("amount") or 0)

    total = _first(raw.get("total"), doc.get("totalAmount"))
    net = _first(raw.get("netAmount"), doc.get("netAmount"))
    issued = _first(raw.get("dateTimeIssued"), doc.get("dateTimeIssued"))
    currency = None
    lines = doc.get("invoiceLines") or []
    if lines:
        currency = ((lines[0].get("unitValue") or {}).get("currencySold"))

    return {
        "eta_uuid": _first(raw.get("uuid"), doc.get("uuid")),
        "supplier_tax_id": _first(raw.get("issuerId"), issuer.get("id")),
        "supplier_name": _first(raw.get("issuerName"), issuer.get("name")),
        "buyer_tax_id": _first(raw.get("receiverId"), receiver.get("id")),
        "buyer_name": _first(raw.get("receiverName"), receiver.get("name")),
        "invoice_number": _first(raw.get("internalId"), raw.get("internalID"), doc.get("internalID")),
        "invoice_date": str(issued)[:10] if issued else None,
        "subtotal": float(net) if net is not None else None,
        "vat_amount": round(vat, 2) if vat is not None else None,
        "total": float(total) if total is not None else None,
        "currency": currency if currency and currency != "EGP" else "EGP",
        "document_type": _first(raw.get("typeName"), doc.get("documentType")),
        "eta_status": raw.get("status"),
    }
