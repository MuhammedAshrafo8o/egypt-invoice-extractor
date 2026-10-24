"""Find and decode QR codes on invoices (ETA e-invoices / e-receipts carry one)."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from PIL import Image

try:  # OpenCV is optional (large); without it QR codes are simply not read.
    import cv2
    import numpy as np
    _detector = cv2.QRCodeDetector()
except ImportError:  # pragma: no cover - depends on the install
    cv2 = np = _detector = None


def is_available() -> bool:
    return _detector is not None


def decode_qr_from_image(image: Image.Image) -> list[str]:
    """Return the decoded text of every QR code found in the image."""
    if _detector is None:
        return []
    rgb = np.array(image.convert("RGB"))
    bgr = cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    found: list[str] = []
    try:
        ok, texts, _, _ = _detector.detectAndDecodeMulti(bgr)
        if ok:
            found.extend(t for t in texts if t)
    except cv2.error:
        pass
    if not found:
        text, _, _ = _detector.detectAndDecode(bgr)
        if text:
            found.append(text)
    return found


def decode_qr_from_pdf(path: Path, max_pages: int = 3) -> list[str]:
    if _detector is None:
        return []
    import pypdfium2 as pdfium

    from ..pdfium_lock import PDFIUM_LOCK

    found: list[str] = []
    for index in range(max_pages):
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                if index >= len(pdf):
                    break
                image = pdf[index].render(scale=2.5).to_pil()
            finally:
                pdf.close()
        found.extend(decode_qr_from_image(image))
        if found:
            break
    return found


def parse_eta_qr(payload: str) -> Optional[dict]:
    """Recognise an Egyptian Tax Authority QR payload.

    Printed e-invoices link to the ETA portal, e.g.
    https://invoicing.eta.gov.eg/print/documents/<UUID>/share/<longId>
    E-receipts encode a similar URL. We keep the UUID and URL so the document
    can later be pulled from the ETA API (see eta_client.py).
    """
    if "eta.gov.eg" not in payload.lower():
        return None
    result: dict = {"eta_url": payload.strip()}
    m = re.search(r"documents/([A-Za-z0-9]+)", payload) or re.search(r"[?&]uuid=([A-Za-z0-9]+)", payload)
    if m:
        result["eta_uuid"] = m.group(1)
    return result
