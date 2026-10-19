"""Generate a realistic *handwritten* Egyptian invoice (pre-printed pad filled in by hand).

    python samples/make_handwritten.py --fonts /path/to/fonts

Needs the free Ruqaa handwriting font from Google Fonts (OFL licence):
  ArefRuqaa-Regular.ttf   https://fonts.google.com/specimen/Aref+Ruqaa
and Pillow built with raqm (for joined Arabic letters).

Writes three files into samples/invoices/:
  10_handwritten_scan.jpg    flat, like a scanner
  11_handwritten_photo.jpg   phone photo on a desk (perspective, shadow, blur)
  handwritten_answer_key.json  the correct values, to compare with what was read

All names and numbers are fictional.
"""
from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(__file__).parent / "invoices"
W, H = 1240, 1700
PAPER = (246, 243, 233)
PRINT_INK = (28, 38, 70)
RED_INK = (175, 35, 40)
PEN = (24, 48, 150)

AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# ----------------------------------------------------------------- the invoice
ITEMS = [  # (description, qty, unit price)
    ("أسمنت بورتلاندي", 40, 115),
    ("سيخ حديد ١٢ مم", 30, 95),
    ("رمل نقلة", 2, 750),
]
SUBTOTAL = sum(q * p for _, q, p in ITEMS)          # 8,950
VAT = round(SUBTOTAL * 0.14)                          # 1,253
TOTAL = SUBTOTAL + VAT                                # 10,203
ANSWER = {
    "supplier_name": "معرض الفتح لمواد البناء",
    "supplier_tax_id": "529317604",
    "buyer_name": "شركة الدلتا للتجارة",
    "buyer_tax_id": "300112455",
    "invoice_number": "0347",
    "invoice_date": "2026-10-12",
    "subtotal": float(SUBTOTAL),
    "vat_rate": 0.14,
    "vat_amount": float(VAT),
    "total": float(TOTAL),
}


def ar(n) -> str:
    return f"{n}".translate(AR_DIGITS)


class Hand:
    """Writes words one by one with the small irregularities of real handwriting."""

    def __init__(self, img: Image.Image, font_path: str, seed: int = 7):
        self.img = img
        self.font_path = font_path
        self.rng = random.Random(seed)

    def _word(self, text: str, size: int, ink) -> Image.Image:
        font = ImageFont.truetype(self.font_path, size)
        probe = ImageDraw.Draw(Image.new("L", (1, 1)))
        box = probe.textbbox((0, 0), text, font=font, direction="rtl", language="ar")
        w, h = box[2] - box[0] + 20, box[3] - box[1] + 30
        layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        alpha = self.rng.randint(205, 245)  # ink pressure
        d.text((10 - box[0], 15 - box[1]), text, font=font, fill=(*ink, alpha), direction="rtl", language="ar")
        return layer.rotate(self.rng.uniform(-2.5, 2.5), resample=Image.BICUBIC, expand=True)

    def write(self, right_x: int, baseline_y: int, text: str, size: int = 44, ink=PEN, slope: float = 0.0):
        """Write `text` right-to-left starting at right_x. Returns the x where writing stopped."""
        x = right_x
        for word in text.split(" "):
            size_j = size + self.rng.randint(-3, 3)
            layer = self._word(word, size_j, ink)
            y = int(baseline_y - layer.height * 0.62 + self.rng.randint(-4, 4) - (right_x - x) * slope)
            x -= layer.width
            self.img.alpha_composite(layer, (int(x), y))
            x -= self.rng.randint(4, 14)  # gap between words
        return x

    def write_ltr(self, left_x: int, baseline_y: int, text: str, size: int = 44, ink=PEN):
        layer = self._word(text, size, ink)
        self.img.alpha_composite(layer, (left_x, int(baseline_y - layer.height * 0.62)))


def printed(draw: ImageDraw.ImageDraw, xy, text, size, font, fill=PRINT_INK, anchor="ra"):
    draw.text(xy, text, font=ImageFont.truetype(font, size), fill=fill, anchor=anchor,
              direction="rtl", language="ar")


def make_invoice(fonts: Path) -> Image.Image:
    ruqaa = str(fonts / "ArefRuqaa-Regular.ttf")
    sans = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    sans_bold = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

    img = Image.new("RGBA", (W, H), (*PAPER, 255))
    d = ImageDraw.Draw(img)

    # ---- pre-printed header (what the print shop puts on the invoice pad)
    d.rectangle((60, 50, W - 60, 300), outline=PRINT_INK, width=3)
    printed(d, (W - 90, 75), "معرض الفتح لمواد البناء", 52, sans_bold)
    printed(d, (W - 90, 150), "أسمنت – حديد – رمل – طوب", 30, sans)
    printed(d, (W - 90, 195), "٢٤ شارع الجيش – طنطا   ت: ٠٤٠٣٣٣٨٧١٢", 26, sans)
    printed(d, (W - 90, 240), "رقم التسجيل الضريبي: 529-317-604   س.ت: 48213", 26, sans)
    printed(d, (300, 85), "فاتورة ضريبية", 40, sans_bold, anchor="ma")
    printed(d, (300, 160), "№ 0347", 46, sans_bold, fill=RED_INK, anchor="ma")

    # ---- form fields
    printed(d, (W - 80, 345), "التاريخ:", 30, sans)
    printed(d, (W - 80, 410), "السيد / السادة:", 30, sans)
    printed(d, (W - 80, 475), "الرقم الضريبي للعميل:", 30, sans)
    for y, x_end in ((385, 1000), (450, 880), (515, 760)):
        for x in range(80, x_end, 14):  # dotted lines
            d.line((x, y, x + 7, y), fill=(120, 125, 140), width=2)

    # ---- items table: الصنف | الكمية | السعر | القيمة (right to left)
    top, row_h, rows = 560, 78, 7
    cols = [W - 60, 700, 520, 330, 60]  # x boundaries from right to left
    d.rectangle((60, top, W - 60, top + row_h * (rows + 1)), outline=PRINT_INK, width=3)
    d.rectangle((60, top, W - 60, top + row_h), fill=(225, 228, 236))
    for x in cols[1:-1]:
        d.line((x, top, x, top + row_h * (rows + 1)), fill=PRINT_INK, width=2)
    for r in range(1, rows + 1):
        d.line((60, top + row_h * r, W - 60, top + row_h * r), fill=PRINT_INK, width=1)
    for (right, left), title in zip(zip(cols, cols[1:]), ("الصنف", "الكمية", "السعر", "القيمة")):
        printed(d, ((right + left) // 2, top + 22), title, 30, sans_bold, anchor="ma")

    # ---- totals box
    ty = top + row_h * (rows + 1) + 30
    labels = ("الإجمالي", "ضريبة القيمة المضافة ١٤٪", "الإجمالي بعد الضريبة")
    for i, label in enumerate(labels):
        y = ty + i * 75
        d.rectangle((60, y, 560, y + 65), outline=PRINT_INK, width=2)
        printed(d, (W - 80, y + 16), label + ":", 30, sans_bold)
    printed(d, (W - 80, ty + 260), "توقيع المستلم: ..................", 26, sans)
    printed(d, (420, ty + 260), "الختم", 26, sans, anchor="ma")

    # ---- handwriting
    hand = Hand(img, ruqaa)
    hand.write(1060, 362, f"{ar(12)} / {ar(10)} / {ar(2026)}", 46, slope=0.01)
    hand.write(900, 428, "شركة الدلتا للتجارة", 50, slope=0.012)
    hand.write(860, 494, f"{ar(300)}-{ar(112)}-{ar(455)}", 46)

    for i, (desc, qty, price) in enumerate(ITEMS, start=1):
        base = top + row_h * i + 52
        hand.write(cols[0] - 25, base, desc, 44, slope=0.008)
        hand.write(cols[1] - 55, base, ar(qty), 46)
        hand.write(cols[2] - 50, base, ar(price), 46)
        hand.write(cols[3] - 45, base, ar(f"{qty * price:,}".replace(",", "")), 46)
    # a struck-through mistake, like real pads
    mistake_y = top + row_h * 4 + 52
    hand.write(cols[0] - 25, mistake_y, "طوب أحمر", 44)
    d = ImageDraw.Draw(img)
    d.line((cols[1] + 40, mistake_y - 10, cols[0] - 20, mistake_y - 22), fill=(*PEN, 230), width=4)

    for i, value in enumerate((SUBTOTAL, VAT, TOTAL)):
        hand.write(520, ty + i * 75 + 46, ar(value), 50)
    # underline the grand total twice, as people do
    d.line((150, ty + 212, 540, ty + 206), fill=(*PEN, 220), width=3)
    d.line((160, ty + 220, 530, ty + 215), fill=(*PEN, 200), width=3)

    # ---- signature scribble
    rng = random.Random(3)
    pts, x0, y0 = [], 820, ty + 330
    for t in range(140):
        a = t / 140 * 5 * math.pi
        pts.append((x0 + t * 1.6 + 25 * math.sin(a * 1.3), y0 + 22 * math.sin(a) + rng.uniform(-2, 2)))
    d.line(pts, fill=(*PEN, 230), width=3, joint="curve")

    # ---- round stamp, slightly faded and rotated
    stamp = Image.new("RGBA", (300, 300), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stamp)
    sd.ellipse((10, 10, 290, 290), outline=(60, 70, 170, 160), width=7)
    sd.ellipse((45, 45, 255, 255), outline=(60, 70, 170, 140), width=3)
    sd.text((150, 125), "معرض الفتح", font=ImageFont.truetype(sans_bold, 34), fill=(60, 70, 170, 150),
            anchor="mm", direction="rtl", language="ar")
    sd.text((150, 175), "طنطا", font=ImageFont.truetype(sans_bold, 30), fill=(60, 70, 170, 150),
            anchor="mm", direction="rtl", language="ar")
    img.alpha_composite(stamp.rotate(-18, resample=Image.BICUBIC), (290, ty + 230))

    return img


# --------------------------------------------------------------------- effects
def paper_texture(img: Image.Image, seed: int = 1) -> Image.Image:
    rng = np.random.default_rng(seed)
    arr = np.asarray(img.convert("RGB")).astype(np.float32)
    grain = rng.normal(0, 4.5, arr.shape[:2])[..., None]
    fibres = cv2.GaussianBlur(rng.normal(0, 10, arr.shape[:2]).astype(np.float32), (0, 0), 6)[..., None]
    arr = np.clip(arr + grain + fibres * 0.6, 0, 255)
    return Image.fromarray(arr.astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.6))


def as_scan(img: Image.Image) -> Image.Image:
    return paper_texture(img).rotate(0.8, resample=Image.BICUBIC, fillcolor=(250, 250, 250))


def as_phone_photo(img: Image.Image, seed: int = 5) -> Image.Image:
    rng = np.random.default_rng(seed)
    paper = np.asarray(paper_texture(img, seed))
    # desk background
    bw, bh = 1500, 2000
    desk = np.zeros((bh, bw, 3), np.float32)
    desk[:] = (92, 70, 52)
    desk += cv2.GaussianBlur(rng.normal(0, 18, (bh, bw)).astype(np.float32), (0, 0), sigmaX=40, sigmaY=3)[..., None]
    # perspective: phone held slightly tilted
    src = np.float32([[0, 0], [W, 0], [W, H], [0, H]])
    dst = np.float32([[150, 140], [1350, 185], [1395, 1860], [95, 1810]])
    m = cv2.getPerspectiveTransform(src, dst)
    warped = cv2.warpPerspective(paper.astype(np.float32), m, (bw, bh))
    mask = cv2.warpPerspective(np.ones((H, W), np.float32), m, (bw, bh))[..., None]
    # soft shadow under the paper
    shadow = cv2.GaussianBlur(np.roll(mask, (18, 14), (0, 1))[..., 0], (0, 0), 14)[..., None]
    out = desk * (1 - 0.55 * shadow)
    out = out * (1 - mask) + warped * mask
    # uneven light: brighter top-left, phone shadow at the bottom-right
    yy, xx = np.mgrid[0:bh, 0:bw].astype(np.float32)
    light = 1.08 - 0.22 * ((xx / bw) * 0.6 + (yy / bh) * 0.7)
    light -= 0.18 * np.exp(-(((xx - 1150) / 380) ** 2 + ((yy - 1750) / 260) ** 2))
    out = np.clip(out * light[..., None] * np.float32([1.0, 0.98, 0.93]), 0, 255)  # warm indoor light
    out = cv2.GaussianBlur(out, (0, 0), 1.1)  # slight focus blur
    out += rng.normal(0, 3.5, out.shape)       # sensor noise
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fonts", required=True, help="folder with ArefRuqaa-Regular.ttf")
    args = parser.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    invoice = make_invoice(Path(args.fonts))
    as_scan(invoice).save(OUT / "10_handwritten_scan.jpg", quality=88)
    as_phone_photo(invoice).save(OUT / "11_handwritten_photo.jpg", quality=72)
    (OUT / "handwritten_answer_key.json").write_text(json.dumps(ANSWER, ensure_ascii=False, indent=2),
                                                     encoding="utf-8")
    print(f"Wrote handwritten samples to {OUT}")


if __name__ == "__main__":
    main()
