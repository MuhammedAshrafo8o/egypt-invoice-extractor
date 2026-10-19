"""Route each file to the right extractor, then validate.

  e-invoice PDF with ETA QR → keep UUID/link (+ text fields from the PDF)
  digital PDF               → pdfplumber text → rule-based parser
  scanned PDF / photo       → Claude vision
  rule-based result incomplete and AI available → AI fills the gaps
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Callable

from PIL import Image

from . import normalize
from .extractors import ai_vision, ocr, pdf_text, qr
from .extractors.text_parser import parse_text
from .models import Invoice
from .validation import ValidationConfig, flag_duplicates, validate_invoice

log = logging.getLogger(__name__)

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
SUPPORTED_EXTS = IMAGE_EXTS | {".pdf"}
CORE_FIELDS = ("supplier_tax_id", "invoice_date", "total", "vat_amount")
_REASON_AR = {
    "Image invoice": "صورة فاتورة",
    "Scanned PDF (no text layer)": "PDF ممسوح ضوئيًا (مفيهوش نص)",
}


def _clean(data: dict) -> dict:
    """Normalise types coming from any extractor."""
    out = dict(data)
    for key in ("subtotal", "vat_amount", "total"):
        if key in out:
            out[key] = normalize.parse_amount(out[key])
    if out.get("vat_rate") is not None:
        rate = normalize.parse_amount(out["vat_rate"])
        out["vat_rate"] = rate / 100 if rate and rate > 1 else rate
    if "invoice_date" in out:
        out["invoice_date"] = normalize.parse_date(out["invoice_date"])
    for key in ("supplier_tax_id", "buyer_tax_id"):
        if key in out:
            out[key] = normalize.digits_only(out[key])
    for key in ("supplier_name", "buyer_name", "invoice_number"):
        if isinstance(out.get(key), str):
            out[key] = out[key].strip() or None
    if isinstance(out.get("currency"), str):
        out["currency"] = out["currency"].strip().upper() or "EGP"
    return out


clean_fields = _clean  # public name


def _missing_core(inv: Invoice) -> list[str]:
    return [f for f in CORE_FIELDS if getattr(inv, f) in (None, "")]


def _apply_ai(inv: Invoice, path: Path, use_ai: bool, reason: str, use_ocr: bool = True) -> None:
    """Read an image / scanned PDF: AI model if configured, otherwise (or if it fails) free OCR."""
    ai_done = False
    if use_ai and ai_vision.is_available():
        ai_done = _read_with_ai(inv, path)
    if ai_done:
        return
    if use_ocr and ocr.is_available():
        _read_with_ocr(inv, path)
        return
    reason_ar = _REASON_AR.get(reason, reason)
    if not use_ai and not use_ocr:
        inv.add_issue("warning", "ai_disabled", f"{reason}; AI extraction and OCR are turned off.",
                      f"{reason_ar}؛ القراءة الأوتوماتيك مقفولة.")
    elif not any(i.code == "ai_failed" for i in inv.issues):
        inv.add_issue("warning", "ai_unavailable",
                      f"{reason}; install Tesseract (free) or set an AI key to read it.",
                      f"{reason_ar}؛ سطّب Tesseract (مجاني) أو ضيف مفتاح ذكاء اصطناعي عشان تتقري.")


def _read_with_ai(inv: Invoice, path: Path) -> bool:
    readable = path
    tmp = None
    if path.suffix.lower() in {".bmp", ".tif", ".tiff"}:  # convert formats the APIs don't take
        tmp = Path(tempfile.mkstemp(suffix=".png")[1])
        Image.open(path).convert("RGB").save(tmp)
        readable = tmp
    try:
        data = ai_vision.extract_with_ai(readable)
    except ai_vision.AIExtractionError as exc:
        inv.add_issue("warning", "ai_failed", f"AI extraction failed: {exc}",
                      f"القراءة بالذكاء الاصطناعي فشلت: {exc}")
        return False
    finally:
        if tmp and tmp.exists():
            tmp.unlink()
    confidence = data.pop("confidence", None)
    filled = inv.merge_missing(_clean(data))
    if inv.source_type == "unknown":
        inv.source_type = "ai_vision"
    elif filled:
        inv.source_type += "+ai_vision"
    if isinstance(confidence, (int, float)) and confidence < 0.7:
        inv.add_issue("warning", "ai_low_confidence", f"AI confidence is low ({confidence:.0%}).",
                      f"ثقة القراءة ضعيفة ({confidence:.0%}) — راجع الأرقام.")
    return True


def _read_with_ocr(inv: Invoice, path: Path) -> None:
    try:
        text = ocr.ocr_file(path)
    except ocr.OCRError as exc:
        inv.add_issue("warning", "ocr_failed", f"OCR failed: {exc}", f"القراءة المجانية (OCR) فشلت: {exc}")
        return
    inv.raw_text = text
    filled = inv.merge_missing(_clean(parse_text(text)))
    if inv.source_type == "unknown":
        inv.source_type = "ocr"
    elif filled:
        inv.source_type += "+ocr"
    inv.add_issue("warning", "ocr_used", "Read with free OCR — check the numbers before approving.",
                  "اتقرت بالقراءة المجانية (OCR) — راجع الأرقام، وخصوصًا الرقم الضريبي، قبل الاعتماد.")


def apply_eta_data(inv: Invoice, data: dict) -> None:
    """ETA data is the official record, so it overrides whatever was read from the file."""
    for name, value in _clean(data).items():
        if name in Invoice.FIELDS and value not in (None, ""):
            setattr(inv, name, value)
    inv.eta_uuid = data.get("eta_uuid") or inv.eta_uuid
    inv.source_type = "eta_api"


def process_file(path: str | Path, use_ai: bool = True, cfg: ValidationConfig | None = None,
                 eta_fetcher: Callable[[str], dict] | None = None, use_ocr: bool = True) -> Invoice:
    """eta_fetcher(uuid) -> field dict; when given, e-invoices are pulled from the ETA API."""
    path = Path(path)
    inv = Invoice(source_file=path.name)
    ext = path.suffix.lower()

    if ext not in SUPPORTED_EXTS:
        inv.add_issue("error", "unsupported_file", f"Unsupported file type '{ext}'.",
                      f"نوع الملف '{ext}' مش مدعوم.")
        inv.status = "error"
        return inv

    try:
        # 1) QR code (ETA e-invoices / e-receipts)
        payloads = qr.decode_qr_from_pdf(path) if ext == ".pdf" else qr.decode_qr_from_image(Image.open(path))
        for payload in payloads:
            eta = qr.parse_eta_qr(payload)
            if eta:
                inv.eta_url = eta.get("eta_url")
                inv.eta_uuid = eta.get("eta_uuid")
                inv.source_type = "eta_qr"
                break

        if inv.eta_uuid and eta_fetcher:
            try:
                apply_eta_data(inv, eta_fetcher(inv.eta_uuid))
            except Exception as exc:  # fall back to reading the file
                inv.add_issue("warning", "eta_fetch_failed", f"Could not fetch from ETA: {exc}",
                              f"ماقدرناش نسحب الفاتورة من المنظومة: {exc}")

        # 2) Digital PDF text
        if inv.source_type == "eta_api":
            pass
        elif ext == ".pdf":
            text = pdf_text.extract_pdf_text(path)
            inv.raw_text = text
            if pdf_text.is_scanned(text):
                _apply_ai(inv, path, use_ai, "Scanned PDF (no text layer)", use_ocr)
            else:
                inv.merge_missing(_clean(parse_text(text)))
                if inv.source_type == "unknown":
                    inv.source_type = "pdf_text"
                missing = _missing_core(inv)
                if missing and use_ai and ai_vision.is_available():
                    _apply_ai(inv, path, use_ai, f"Text parser missed {', '.join(missing)}", use_ocr=False)
        # 3) Photo
        else:
            _apply_ai(inv, path, use_ai, "Image invoice", use_ocr)
    except Exception as exc:  # keep the batch running; report the file
        log.exception("Failed to process %s", path)
        inv.add_issue("error", "processing_failed", f"{type(exc).__name__}: {exc}",
                      f"حصل خطأ أثناء قراءة الملف: {exc}")

    if inv.eta_uuid and inv.source_type != "eta_api":
        inv.add_issue("warning", "eta_document",
                      "Official e-invoice — pull the exact data from the ETA API to skip manual review.",
                      "فاتورة إلكترونية رسمية — اربط حساب المنظومة عشان بياناتها تتسحب بالظبط.")
    return validate_invoice(inv, cfg)


def process_many(paths, use_ai: bool = True, cfg: ValidationConfig | None = None,
                 use_ocr: bool = True) -> list[Invoice]:
    invoices = [process_file(p, use_ai=use_ai, cfg=cfg, use_ocr=use_ocr) for p in paths]
    flag_duplicates(invoices)
    return invoices


def vat_summary(invoices: list[Invoice]) -> dict:
    """Totals for the monthly VAT return, from invoices that passed (ok/review)."""
    usable = [i for i in invoices if i.status in ("ok", "review") and i.currency == "EGP"]
    return {
        "invoices_total": len(invoices),
        "invoices_usable": len(usable),
        "invoices_need_fixing": sum(1 for i in invoices if i.status == "error"),
        "invoices_to_review": sum(1 for i in invoices if i.status == "review"),
        "subtotal_sum": round(sum(i.subtotal or 0 for i in usable), 2),
        "vat_sum": round(sum(i.vat_amount or 0 for i in usable), 2),
        "total_sum": round(sum(i.total or 0 for i in usable), 2),
        "non_egp_invoices": sum(1 for i in invoices if i.currency != "EGP"),
    }
