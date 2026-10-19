"""Extract invoice fields from images / scanned PDFs with Claude (vision).

Uses the Anthropic Messages API over plain HTTPS (standard library only), so
there is no SDK dependency. Set ANTHROPIC_API_KEY to enable it.
"""
from __future__ import annotations

import base64
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"  # override base with ANTHROPIC_BASE_URL
DEFAULT_MODEL = "claude-sonnet-5-5"

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
}

PROMPT = """You are reading an invoice issued in Egypt. It may be in Arabic, English or both,
printed or handwritten. Extract the fields below and answer with ONE JSON object only,
no prose, no markdown fences.

{
  "supplier_name": string|null,      // seller / المورد / البائع
  "supplier_tax_id": string|null,    // seller's tax registration number (رقم التسجيل الضريبي), digits only
  "buyer_name": string|null,
  "buyer_tax_id": string|null,       // digits only
  "invoice_number": string|null,
  "invoice_date": string|null,       // ISO yyyy-mm-dd; Egyptian dates are day/month/year
  "currency": string,                // ISO code, default "EGP"
  "subtotal": number|null,           // total BEFORE VAT
  "vat_rate": number|null,           // as a fraction, 14% -> 0.14
  "vat_amount": number|null,
  "total": number|null,              // grand total including VAT
  "confidence": number               // 0..1, your confidence in the extraction overall
}

Rules: convert Arabic-Indic digits (٠-٩) to 0-9. Never guess a value you cannot read; use null.
Do not compute missing values, only report what is written."""


class AIExtractionError(RuntimeError):
    pass


def is_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def _content_block(path: Path) -> dict:
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if not media_type:
        raise AIExtractionError(f"Unsupported file type for AI extraction: {path.suffix}")
    data = base64.standard_b64encode(path.read_bytes()).decode("ascii")
    block_type = "document" if media_type == "application/pdf" else "image"
    return {"type": block_type, "source": {"type": "base64", "media_type": media_type, "data": data}}


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise AIExtractionError("Model response did not contain JSON")
    return json.loads(match.group(0))


def extract_with_ai(path: Path, model: str | None = None, timeout: int = 90) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AIExtractionError("ANTHROPIC_API_KEY is not set")

    body = {
        "model": model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
        "max_tokens": 1024,
        "messages": [{
            "role": "user",
            "content": [_content_block(path), {"type": "text", "text": PROMPT}],
        }],
    }
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    request = urllib.request.Request(
        f"{base}/v1/messages" if base else API_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        raise AIExtractionError(f"API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AIExtractionError(f"Network error: {exc.reason}") from exc

    text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")
    return _parse_json(text)
