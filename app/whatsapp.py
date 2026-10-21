"""WhatsApp Cloud API: clients send invoice photos/PDFs to one business number.

Flow: Meta → POST /webhooks/whatsapp → find client by sender phone → download media →
process → store → reply with what was read.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import mimetypes
from typing import Optional

import requests

from .config import Settings
from .db import Database
from .services import InvoiceService

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
MEDIA_EXTS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "application/pdf": ".pdf",
}

HELP_TEXT = (
    "أهلًا 👋\n"
    "ابعت صورة الفاتورة أو ملف PDF هنا، وهتوصل للمحاسب بتاعك على طول.\n"
    "• صوّر الفاتورة كاملة وفي إضاءة كويسة.\n"
    "• فاتورة واحدة في كل صورة."
)
UNKNOWN_SENDER_TEXT = "الرقم ده مش مسجل عندنا. كلّم المحاسب بتاعك عشان يضيف رقمك."
STATUS_TEXT = {
    "ok": "✅ الفاتورة اتسجلت وبياناتها سليمة.",
    "review": "🟡 الفاتورة اتسجلت، والمحاسب هيراجع شوية تفاصيل.",
    "error": "🔴 الفاتورة اتسجلت بس فيها مشكلة، والمحاسب هيراجعها. لو الصورة مش واضحة ابعتها تاني.",
}


def verify_signature(app_secret: str, body: bytes, header: Optional[str]) -> bool:
    if not app_secret:
        return True  # signature check disabled (development)
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


class WhatsAppClient:
    def __init__(self, settings: Settings, session: Optional[requests.Session] = None):
        self.settings = settings
        self.session = session or requests.Session()

    @property
    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self.settings.whatsapp_access_token}"}

    def download_media(self, media_id: str) -> tuple[bytes, str]:
        base = f"{GRAPH}/{self.settings.whatsapp_api_version}"
        meta = self.session.get(f"{base}/{media_id}", headers=self._headers, timeout=30)
        meta.raise_for_status()
        info = meta.json()
        data = self.session.get(info["url"], headers=self._headers, timeout=60)
        data.raise_for_status()
        return data.content, info.get("mime_type", "")

    def send_text(self, to: str, text: str) -> None:
        if not self.settings.whatsapp_enabled:
            log.info("WhatsApp reply (not sent, not configured) to %s: %s", to, text)
            return
        url = f"{GRAPH}/{self.settings.whatsapp_api_version}/{self.settings.whatsapp_phone_number_id}/messages"
        response = self.session.post(url, headers=self._headers, timeout=30, json={
            "messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text},
        })
        if response.status_code >= 400:
            log.warning("WhatsApp send failed %s: %s", response.status_code, response.text[:300])


def extract_messages(payload: dict) -> list[dict]:
    messages = []
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages.extend(value.get("messages", []))
    return messages


def _reply_for(invoice: dict) -> str:
    lines = [STATUS_TEXT.get(invoice["status"], "")]
    if invoice.get("supplier_name"):
        lines.append(f"المورد: {invoice['supplier_name']}")
    if invoice.get("invoice_number"):
        lines.append(f"رقم الفاتورة: {invoice['invoice_number']}")
    if invoice.get("total") is not None:
        lines.append(f"الإجمالي: {invoice['total']:,.2f} {invoice.get('currency') or 'EGP'}")
    return "\n".join(line for line in lines if line)


def handle_message(message: dict, db: Database, service: InvoiceService, client_api: WhatsAppClient) -> None:
    """Process one incoming message. Safe to call twice for the same message (deduplicated)."""
    message_id = message.get("id")
    sender = message.get("from", "")
    if not message_id or not db.record_whatsapp(message_id, sender):
        return

    client = db.client_by_phone(sender)
    if not client:
        client_api.send_text(sender, UNKNOWN_SENDER_TEXT)
        return

    kind = message.get("type")
    if kind not in ("image", "document"):
        client_api.send_text(sender, HELP_TEXT)
        return

    media = message.get(kind) or {}
    try:
        content, mime = client_api.download_media(media["id"])
        mime = media.get("mime_type") or mime
        ext = MEDIA_EXTS.get(mime.split(";")[0].strip()) or mimetypes.guess_extension(mime or "") or ""
        filename = media.get("filename") or f"whatsapp_{message_id[-8:]}{ext}"
        if not filename.lower().endswith(tuple(MEDIA_EXTS.values())) and ext:
            filename += ext
        # Into the same queue as uploads: a client sending 40 photos at once doesn't fire 40 readings
        # at the same moment (AI rate limits). The reply goes out when the file has been read.
        result = service.queue_files(client, [(filename, content)], channel="whatsapp",
                                     reply_to=sender, message_id=message_id)
    except Exception:
        log.exception("WhatsApp media processing failed")
        client_api.send_text(sender, "⚠️ حصلت مشكلة في استلام الملف. جرّب تبعته تاني بعد شوية.")
        return
    for problem in result["skipped"] + result["failed"]:
        client_api.send_text(sender, f"⚠️ {problem}")


def reply_when_read(job: dict, db: Database, client_api: "WhatsAppClient") -> None:
    """Job listener: tell the client what was read from the photo they sent."""
    if job.get("channel") != "whatsapp" or not job.get("reply_to"):
        return
    if job.get("state") != "done" or not job.get("invoice_id"):
        client_api.send_text(job["reply_to"], "⚠️ ماقدرناش نقرا الملف ده. ابعته تاني، أو كلّم المحاسب.")
        return
    if job.get("message_id"):
        db.link_whatsapp_invoice(job["message_id"], job["invoice_id"])
    client_api.send_text(job["reply_to"], _reply_for(db.get_invoice(job["invoice_id"])))
