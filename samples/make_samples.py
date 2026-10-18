"""Generate synthetic sample invoices for trying the extractor.

    python samples/make_samples.py   →  writes files into samples/invoices/

All companies, numbers and links are fictional.
"""
from __future__ import annotations

import io
from datetime import date, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A5
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

OUT = Path(__file__).parent / "invoices"


def _qr_png(text: str) -> Image.Image:
    encoder = cv2.QRCodeEncoder_create()
    qr = encoder.encode(text)
    qr = cv2.resize(qr, (qr.shape[1] * 8, qr.shape[0] * 8), interpolation=cv2.INTER_NEAREST)
    qr = np.pad(qr, 32, constant_values=255)
    return Image.fromarray(qr)


def _pdf(path: Path, lines: list[str], qr_text: str | None = None) -> None:
    c = canvas.Canvas(str(path), pagesize=A5)
    width, height = A5
    y = height - 50
    for i, line in enumerate(lines):
        c.setFont("Helvetica-Bold" if i == 0 else "Helvetica", 14 if i == 0 else 10)
        c.drawString(40, y, line)
        y -= 22 if i == 0 else 16
    if qr_text:
        c.drawImage(ImageReader(_qr_png(qr_text)), width - 150, 40, 110, 110)
    c.save()


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    today = date.today()
    d1 = (today - timedelta(days=12)).strftime("%d/%m/%Y")
    d2 = (today - timedelta(days=5)).strftime("%d/%m/%Y")

    # 1. Clean digital invoice — should come out "ok"
    _pdf(OUT / "01_digital_ok.pdf", [
        "Nile Office Supplies LLC",
        "Tax Registration No: 512-334-901",
        "Invoice No: INV-2026-0147",
        f"Invoice Date: {d1}",
        "Bill to: Delta Trading Co.",
        "Customer Tax ID: 300-112-455",
        "",
        "A4 paper boxes x 20 ............ 6,000.00",
        "Printer toner x 4 ............... 4,000.00",
        "",
        "Subtotal: 10,000.00",
        "VAT 14%: 1,400.00",
        "Total incl. VAT: 11,400.00 EGP",
    ])

    # 2. Numbers don't add up — should be "error"
    _pdf(OUT / "02_total_mismatch.pdf", [
        "Cairo Print House",
        "Tax Registration No: 410229873",
        "Invoice No: CP-889",
        f"Date: {d2}",
        "Subtotal: 2,500.00",
        "VAT 14%: 350.00",
        "Total: 3,050.00",
    ])

    # 3. Same invoice sent twice — the copy should be flagged as duplicate
    (OUT / "03_duplicate_of_01.pdf").write_bytes((OUT / "01_digital_ok.pdf").read_bytes())

    # 4. Official e-invoice with an ETA QR code (fictional UUID)
    _pdf(OUT / "04_eta_einvoice.pdf", [
        "Alex Logistics S.A.E",
        "Tax Registration No: 205-778-341",
        "Invoice No: EI-55021",
        f"Issue Date: {d2}",
        "Net amount: 7,250.00",
        "VAT 14%: 1,015.00",
        "Grand total: 8,265.00",
    ], qr_text="https://invoicing.eta.gov.eg/print/documents/SAMPLE7Q2K9XW4D1M8FZ0E3R6T5Y/share/DEMO")

    # 5. Photo of a paper invoice — needs AI (ANTHROPIC_API_KEY)
    img = Image.new("RGB", (900, 1100), "white")
    draw = ImageDraw.Draw(img)
    text = [
        "Giza Building Materials", "", "Tax Reg No: 377-901-224", "Invoice No: GB-3301",
        f"Date: {d1}", "", "Cement 50 bags ........ 12,500.00", "",
        "Subtotal: 12,500.00", "VAT 14%: 1,750.00", "TOTAL: 14,250.00",
    ]
    for i, line in enumerate(text):
        draw.text((60, 80 + i * 60), line, fill="black", font_size=34 if i == 0 else 28)
    img = img.rotate(2, expand=True, fillcolor="white")
    img.save(OUT / "05_photo.jpg", quality=85)

    # 6. Scanned PDF (image only, no text layer) — needs AI
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    c = canvas.Canvas(str(OUT / "06_scanned.pdf"), pagesize=A5)
    c.drawImage(ImageReader(io.BytesIO(buf.getvalue())), 20, 20, A5[0] - 40, A5[1] - 40)
    c.save()

    print(f"Wrote samples to {OUT}")


if __name__ == "__main__":
    main()
