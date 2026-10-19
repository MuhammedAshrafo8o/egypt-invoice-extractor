"""Generate synthetic sample invoices for trying the extractor.

    python samples/make_samples.py   →  writes files into samples/invoices/

All companies, numbers and links are fictional.
"""
from __future__ import annotations

import io
import sys
from datetime import date, timedelta
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import A5
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

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

    # 7 & 8. Arabic invoices: normal text order, and "visual order" (how many Arabic PDFs
    # store text, so extraction comes out reversed — the extractor repairs it).
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    font = _arabic_font()
    if font:
        pdfmetrics.registerFont(TTFont("Arabic", font))
        d3 = (today - timedelta(days=8)).strftime("%d/%m/%Y")
        arabic = [
            "شركة الأهرام للتوريدات الهندسية",
            "رقم التسجيل الضريبي: 488-201-736",
            "فاتورة رقم: 2026/311",
            f"تاريخ الفاتورة: {d3}",
            "اسم العميل: شركة الدلتا للتجارة",
            "الرقم الضريبي للعميل: 300-112-455",
            "الإجمالي قبل الضريبة: 4,800.00",
            "ضريبة القيمة المضافة 14%: 672.00",
            "الإجمالي شامل الضريبة: 5,472.00 جنيه",
        ]
        from invoice_extractor.extractors.pdf_text import fix_reversed_line

        for name, transform in (("07_arabic.pdf", lambda t: t), ("08_arabic_visual_order.pdf", fix_reversed_line)):
            c = canvas.Canvas(str(OUT / name), pagesize=A5)
            y = A5[1] - 50
            for i, line in enumerate(arabic):
                c.setFont("Arabic", 13 if i == 0 else 10)
                c.drawRightString(A5[0] - 40, y, transform(line))
                y -= 22 if i == 0 else 17
            c.save()

        # 9. Phone photo of a printed Arabic invoice (properly joined Arabic needs Pillow with raqm).
        from PIL import ImageFont, features

        if features.check("raqm"):
            photo = Image.new("RGB", (1000, 1150), (250, 249, 245))
            draw = ImageDraw.Draw(photo)
            big, small = ImageFont.truetype(font, 38), ImageFont.truetype(font, 30)
            d4 = (today - timedelta(days=4)).strftime("%d/%m/%Y")
            lines = [
                "مؤسسة النور للأدوات المكتبية", "",
                "رقم التسجيل الضريبي: \u2066615-904-382\u2069", "فاتورة رقم: 7781", f"التاريخ: {d4}", "",
                "الإجمالي قبل الضريبة: 2,300.00", "ضريبة القيمة المضافة 14%: 322.00",
                "الإجمالي شامل الضريبة: 2,622.00",
            ]
            for i, line in enumerate(lines):
                if line:
                    draw.text((940, 90 + i * 85), line, fill=(25, 25, 25), font=big if i == 0 else small,
                              anchor="ra", direction="rtl", language="ar")
            photo = photo.rotate(-1.5, expand=True, fillcolor=(235, 233, 228), resample=Image.BICUBIC)
            photo.save(OUT / "09_arabic_photo.jpg", quality=80)

    print(f"Wrote samples to {OUT}")


def _arabic_font() -> str | None:
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "C:/Windows/Fonts/arial.ttf",
        "/Library/Fonts/Arial Unicode.ttf",
        "/System/Library/Fonts/Supplemental/Arial.ttf",
    ]
    return next((c for c in candidates if Path(c).exists()), None)


if __name__ == "__main__":
    main()
