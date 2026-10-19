"""Extract invoice fields from images / scanned PDFs with a vision AI model.

Providers (plain HTTPS, standard library only — no SDKs):
  anthropic : Claude              ANTHROPIC_API_KEY
  gemini    : Google Gemini       GEMINI_API_KEY      (has a free tier)
  openai    : any OpenAI-compatible endpoint
              OPENAI_BASE_URL + OPENAI_MODEL (+ OPENAI_API_KEY)
              e.g. OpenRouter (free models), Groq, or Ollama running locally (fully free)

AI_PROVIDER picks one explicitly; by default the first one configured is used.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional

API_URL = "https://api.anthropic.com/v1/messages"  # override base with ANTHROPIC_BASE_URL
DEFAULT_MODEL = "claude-sonnet-5-5"
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"  # override with GEMINI_BASE_URL
GEMINI_DEFAULT_MODEL = "gemini-flash-latest"

MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".gif": "image/gif", ".pdf": "application/pdf",
}
PROVIDER_NAMES = {"anthropic": "Claude", "gemini": "Gemini", "openai": "OpenAI-compatible"}

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


# --------------------------------------------------------------------- config
def _configured() -> list[str]:
    found = []
    if os.environ.get("ANTHROPIC_API_KEY"):
        found.append("anthropic")
    if os.environ.get("GEMINI_API_KEY"):
        found.append("gemini")
    if os.environ.get("OPENAI_BASE_URL") and os.environ.get("OPENAI_MODEL"):
        found.append("openai")
    return found


def provider() -> Optional[str]:
    """The provider that will be used, or None if no AI is configured."""
    choice = os.environ.get("AI_PROVIDER", "").strip().lower()
    configured = _configured()
    if choice:
        return choice if choice in configured else None
    return configured[0] if configured else None


def provider_label() -> Optional[str]:
    p = provider()
    return PROVIDER_NAMES.get(p) if p else None


def is_available() -> bool:
    return provider() is not None


# ------------------------------------------------------------------- helpers
def _media_type(path: Path) -> str:
    media_type = MEDIA_TYPES.get(path.suffix.lower())
    if not media_type:
        raise AIExtractionError(f"Unsupported file type for AI extraction: {path.suffix}")
    return media_type


def _b64(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("ascii")


def _content_block(path: Path) -> dict:
    media_type = _media_type(path)
    block_type = "document" if media_type == "application/pdf" else "image"
    return {"type": block_type, "source": {"type": "base64", "media_type": media_type,
                                           "data": _b64(path.read_bytes())}}


def _pdf_pages_as_png(path: Path, max_pages: int = 2) -> list[bytes]:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        out = []
        for i in range(min(len(pdf), max_pages)):
            buf = io.BytesIO()
            pdf[i].render(scale=2).to_pil().save(buf, format="PNG")
            out.append(buf.getvalue())
        return out
    finally:
        pdf.close()


def _parse_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise AIExtractionError("Model response did not contain JSON")
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise AIExtractionError(f"Model returned invalid JSON: {exc}") from exc


def _post(url: str, body: dict, headers: dict, timeout: int) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json", **headers}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:500]
        if exc.code == 429:
            raise AIExtractionError(f"Rate limit reached (free tiers allow few requests per minute): {detail}") from exc
        raise AIExtractionError(f"API error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AIExtractionError(f"Network error: {exc.reason}") from exc


# ------------------------------------------------------------------ providers
def _anthropic(path: Path, model: Optional[str], timeout: int) -> str:
    base = os.environ.get("ANTHROPIC_BASE_URL", "").rstrip("/")
    payload = _post(
        f"{base}/v1/messages" if base else API_URL,
        {
            "model": model or os.environ.get("ANTHROPIC_MODEL", DEFAULT_MODEL),
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [_content_block(path), {"type": "text", "text": PROMPT}]}],
        },
        {"x-api-key": os.environ["ANTHROPIC_API_KEY"], "anthropic-version": "2023-06-01"},
        timeout,
    )
    return "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text")


def _gemini(path: Path, model: Optional[str], timeout: int) -> str:
    base = os.environ.get("GEMINI_BASE_URL", GEMINI_URL).rstrip("/")
    model = model or os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL)
    payload = _post(
        f"{base}/models/{model}:generateContent",
        {
            "contents": [{"role": "user", "parts": [
                {"inline_data": {"mime_type": _media_type(path), "data": _b64(path.read_bytes())}},
                {"text": PROMPT},
            ]}],
            "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
        },
        {"x-goog-api-key": os.environ["GEMINI_API_KEY"]},
        timeout,
    )
    candidates = payload.get("candidates") or []
    if not candidates:
        raise AIExtractionError(f"Gemini returned no answer: {json.dumps(payload)[:300]}")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(p.get("text", "") for p in parts)


def _openai_compatible(path: Path, model: Optional[str], timeout: int) -> str:
    base = os.environ["OPENAI_BASE_URL"].rstrip("/")
    if path.suffix.lower() == ".pdf":  # most OpenAI-compatible servers only take images
        images = [("image/png", data) for data in _pdf_pages_as_png(path)]
    else:
        images = [(_media_type(path), path.read_bytes())]
    content = [{"type": "text", "text": PROMPT}] + [
        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{_b64(data)}"}} for mime, data in images
    ]
    headers = {}
    if os.environ.get("OPENAI_API_KEY"):
        headers["Authorization"] = f"Bearer {os.environ['OPENAI_API_KEY']}"
    payload = _post(
        f"{base}/chat/completions",
        {"model": model or os.environ["OPENAI_MODEL"], "temperature": 0,
         "messages": [{"role": "user", "content": content}]},
        headers, timeout,
    )
    choices = payload.get("choices") or []
    if not choices:
        raise AIExtractionError(f"No answer from model: {json.dumps(payload)[:300]}")
    message = choices[0].get("message") or {}
    text = message.get("content")
    if isinstance(text, list):
        text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
    return text or ""


_PROVIDERS = {"anthropic": _anthropic, "gemini": _gemini, "openai": _openai_compatible}


def extract_with_ai(path: Path, model: str | None = None, timeout: int = 120) -> dict:
    name = provider()
    if not name:
        raise AIExtractionError("No AI provider configured (ANTHROPIC_API_KEY, GEMINI_API_KEY or OPENAI_BASE_URL)")
    return _parse_json(_PROVIDERS[name](Path(path), model, timeout))
