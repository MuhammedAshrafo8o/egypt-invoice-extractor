"""A local stand-in for the ETA API, used when ETA_ENV=demo (and in tests).

It never talks to the tax authority. It checks what the real one would check first
(the totals add up, the document is signed over the right serialized text) and keeps
what was "submitted" in a JSON file so the whole flow can be tried without credentials.
"""
from __future__ import annotations

import base64
import hashlib
import json
import threading
import uuid as uuidlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from .eta_client import ETAError
from .eta_issue import document_hash_input

_LOCK = threading.Lock()


class DemoETAClient:
    def __init__(self, client_id: str = "", client_secret: str = "", env: str = "demo",
                 store: Optional[Path] = None, taxpayer: str = "", **_):
        self.store = Path(store or "data/eta_demo.json")
        self.taxpayer = taxpayer  # like a real login: only this taxpayer's sent/received documents

    # ------------------------------------------------------------ storage
    def _load(self) -> dict:
        try:
            return json.loads(self.store.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"documents": {}}

    def _save(self, data: dict) -> None:
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    def add_received(self, document: dict) -> str:
        """Seed a document 'issued to' a client (used by the demo data and tests)."""
        with _LOCK:
            data = self._load()
            doc_uuid = uuidlib.uuid4().hex.upper()[:26]
            data["documents"][doc_uuid] = {"document": document, "status": "Valid",
                                           "longId": uuidlib.uuid4().hex, "errors": []}
            self._save(data)
        return doc_uuid

    # ---------------------------------------------------------------- API
    def token(self) -> str:
        return "demo-token"

    def submit_documents(self, documents: list[dict]) -> dict:
        accepted, rejected = [], []
        with _LOCK:
            data = self._load()
            for doc in documents:
                errors = self._check(doc)
                internal = doc.get("internalID")
                if errors and errors[0].startswith("structure:"):
                    rejected.append({"internalId": internal, "error": {
                        "message": "Validation error", "details": [{"message": e[10:]} for e in errors]}})
                    continue
                doc_uuid = uuidlib.uuid4().hex.upper()[:26]
                long_id = uuidlib.uuid4().hex
                data["documents"][doc_uuid] = {"document": doc, "status": "Invalid" if errors else "Valid",
                                               "longId": long_id, "errors": errors,
                                               "submitted": datetime.now(timezone.utc).isoformat()}
                accepted.append({"uuid": doc_uuid, "longId": long_id, "internalId": internal})
            self._save(data)
        return {"submissionId": uuidlib.uuid4().hex.upper()[:26], "acceptedDocuments": accepted,
                "rejectedDocuments": rejected}

    def get_document_details(self, doc_uuid: str) -> dict:
        entry = self._load()["documents"].get(doc_uuid)
        if not entry:
            raise ETAError("Document not found.")
        steps = [{"name": "Demo checks", "status": "Invalid",
                  "error": {"details": [{"message": e} for e in entry["errors"]]}}] if entry["errors"] else []
        return {"uuid": doc_uuid, "status": entry["status"], "longId": entry["longId"],
                "validationResults": {"status": entry["status"], "validationSteps": steps}}

    def get_document(self, doc_uuid: str) -> dict:
        entry = self._load()["documents"].get(doc_uuid)
        if not entry:
            raise ETAError("Document not found.")
        return {"uuid": doc_uuid, "status": entry["status"], "document": entry["document"]}

    def search_documents(self, date_from, date_to, direction=None, page_size=100) -> Iterator[dict]:
        for doc_uuid, entry in self._load()["documents"].items():
            doc = entry["document"]
            if self.taxpayer and self.taxpayer not in ((doc.get("issuer") or {}).get("id"),
                                                       (doc.get("receiver") or {}).get("id")):
                continue
            issued = str(doc.get("dateTimeIssued", ""))[:10]
            if date_from.isoformat() <= issued <= date_to.isoformat():
                yield {"uuid": doc_uuid, "status": entry["status"]}

    def fetch_fields(self, doc_uuid: str) -> dict:
        from .eta_client import map_document
        return map_document(self.get_document(doc_uuid))

    # ------------------------------------------------------------- checks
    @staticmethod
    def _check(doc: dict) -> list[str]:
        errors = []
        for key in ("issuer", "receiver", "documentType", "dateTimeIssued", "taxpayerActivityCode",
                    "internalID", "invoiceLines"):
            if not doc.get(key):
                errors.append(f"structure:{key} is required")
        if errors:
            return errors
        sigs = [s for s in doc.get("signatures") or [] if s.get("signatureType") == "I"]
        if not sigs:
            return ["structure:Issuer signature is missing"]
        try:
            cms = base64.b64decode(sigs[0]["value"])
        except (ValueError, KeyError):
            return ["structure:Signature is not base64"]
        digest = hashlib.sha256(document_hash_input(doc)).digest()
        if digest not in cms:
            errors.append("التوقيع مش على نفس محتوى الفاتورة (Signature does not match the document).")
        lines = doc["invoiceLines"]
        net = round(sum(l["netTotal"] for l in lines), 5)
        tax = round(sum(t["amount"] for l in lines for t in l.get("taxableItems") or []), 5)
        for n, l in enumerate(lines, 1):
            expected = round(l["netTotal"] + sum(t["amount"] for t in l.get("taxableItems") or []), 5)
            if abs(expected - l["total"]) > 0.00001:
                errors.append(f"Line {n}: total does not equal net + taxes.")
        if abs(net - doc["netAmount"]) > 0.00001:
            errors.append("netAmount does not equal the sum of line net totals.")
        if abs(round(net + tax, 5) - doc["totalAmount"]) > 0.00001:
            errors.append("totalAmount does not equal netAmount + taxes.")
        if doc["issuer"].get("id") == doc["receiver"].get("id"):
            errors.append("Issuer and receiver cannot be the same.")
        return errors
