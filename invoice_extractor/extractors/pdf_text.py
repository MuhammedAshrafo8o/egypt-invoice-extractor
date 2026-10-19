"""Text extraction from digital (non-scanned) PDFs, including reversed-Arabic repair."""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from ..normalize import normalize_text

# Below this many characters we treat the PDF as scanned (an image inside a PDF).
MIN_TEXT_CHARS = 40

# Common invoice words (normalized form) used to detect reversed Arabic text.
_PROBE_WORDS = (
    "الفاتوره", "فاتوره", "الضريبه", "الضريبي", "الاجمالي", "رقم", "تاريخ",
    "شركه", "المورد", "العميل", "القيمه", "المضافه", "المبلغ", "التسجيل",
)
_ARABIC = re.compile(r"[؀-ۿ]")
_NON_ARABIC_RUN = re.compile(r"[^؀-ۿ\s]+")


def looks_reversed(text: str) -> bool:
    """Many PDF generators store Arabic in visual order, so extraction comes out backwards."""
    norm = normalize_text(text)
    normal = sum(norm.count(w) for w in _PROBE_WORDS)
    reversed_hits = sum(norm.count(w[::-1]) for w in _PROBE_WORDS)
    return reversed_hits >= 2 and reversed_hits > normal


def fix_reversed_line(line: str) -> str:
    if not _ARABIC.search(line):
        return line
    flipped = line[::-1]
    # Digits / Latin / punctuation runs were reversed too — flip them back.
    flipped = _NON_ARABIC_RUN.sub(lambda m: m.group(0)[::-1], flipped)
    # Mirror brackets that were swapped by the reversal.
    return flipped.translate(str.maketrans("()[]{}<>", ")(][}{><"))


def fix_reversed_arabic(text: str) -> str:
    if not looks_reversed(text):
        return text
    return "\n".join(fix_reversed_line(line) for line in text.splitlines())


def extract_pdf_text(path: Path, max_pages: int = 5) -> str:
    parts: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages[:max_pages]:
            parts.append(page.extract_text() or "")
    return fix_reversed_arabic("\n".join(parts).strip())


def is_scanned(text: str) -> bool:
    return len(text.replace(" ", "").replace("\n", "")) < MIN_TEXT_CHARS
