"""Text extraction from digital (non-scanned) PDFs."""
from __future__ import annotations

from pathlib import Path

import pdfplumber

# Below this many characters we treat the PDF as scanned (an image inside a PDF).
MIN_TEXT_CHARS = 40


def extract_pdf_text(path: Path, max_pages: int = 5) -> str:
    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[:max_pages]:
            parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()


def is_scanned(text: str) -> bool:
    return len(text.replace(" ", "").replace("\n", "")) < MIN_TEXT_CHARS
