"""Helpers for Arabic text, numbers and dates."""
from __future__ import annotations

import re
from datetime import date, datetime
from typing import Optional

_DIGIT_MAP = str.maketrans(
    "٠١٢٣٤٥٦٧٨٩۰۱۲۳۴۵۶۷۸۹٫٬،",
    "01234567890123456789.,,",
)

# Tashkeel (diacritics) and tatweel, which break simple label matching.
_ARABIC_MARKS = re.compile(r"[ؐ-ًؚ-ٰٟـ]")


def normalize_text(text: str) -> str:
    """Convert Arabic-Indic digits to Latin, drop diacritics, unify letters."""
    if not text:
        return ""
    text = text.translate(_DIGIT_MAP)
    text = _ARABIC_MARKS.sub("", text)
    text = re.sub("[إأآ]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه")
    text = text.replace("‏", "").replace("‎", "")
    return text


def parse_amount(value) -> Optional[float]:
    """Parse '1,234.50', '١٬٢٣٤٫٥٠', 'EGP 1234' into a float."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    s = normalize_text(str(value))
    match = re.search(r"-?\d(?:[\d.,]*\d)?", s)
    if not match:
        return None
    number = match.group(0)
    if "," in number and "." in number:
        # Whichever separator comes last is the decimal point: 1,234.50 / 1.234,50
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        # 1,234 / 1,234,567 → thousands; 12,5 / 12,50 → decimal comma
        number = number.replace(",", "" if re.fullmatch(r"-?\d{1,3}(?:,\d{3})+", number) else ".")
    elif number.count(".") > 1:
        number = number.replace(".", "")  # 1.234.567
    try:
        return round(float(number), 2)
    except ValueError:
        return None


def digits_only(value) -> Optional[str]:
    if value is None:
        return None
    s = re.sub(r"\D", "", normalize_text(str(value)))
    return s or None


_DATE_FORMATS = (
    "%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d",
    "%d/%m/%y", "%d-%m-%y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ",
    "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
)


def parse_date(value) -> Optional[str]:
    """Return an ISO date (yyyy-mm-dd) or None. Assumes day-first (Egyptian style)."""
    if value is None:
        return None
    if isinstance(value, (date, datetime)):
        return value.strftime("%Y-%m-%d")
    s = normalize_text(str(value)).strip()
    match = re.search(
        r"\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}Z?)?|\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4}"
        r"|\d{1,2} [A-Za-z]{3,9} \d{4}|[A-Za-z]{3,9} \d{1,2}, \d{4}",
        s,
    )
    if match:
        s = match.group(0)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None
