"""python -m app                 → run the server
python -m app demo            → create a demo account with a client and sample invoices
python -m app create-user     → create an accountant account
python -m app setup-ocr       → download Arabic/English data for the free OCR (Tesseract)
"""
from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path


def run_server(host: str, port: int, reload: bool) -> None:
    import uvicorn

    print(f"\n  ▶ افتح المتصفح على:  http://localhost:{port}\n")
    uvicorn.run("app.main:create_app", factory=True, host=host, port=port, reload=reload)


def create_demo() -> None:
    from .config import load_settings
    from .db import Database
    from .services import InvoiceService

    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "samples"))
    import make_samples  # noqa: E402

    make_samples.main()
    settings = load_settings()
    db = Database(settings.database_path)
    email, password = "demo@example.com", "demo1234"
    if db.email_exists(email):
        print(f"الحساب التجريبي موجود بالفعل: {email} / {password}")
        return
    uid = db.create_accountant("مكتب تجريبي", email, password)
    service = InvoiceService(db, settings)
    client_id = db.create_client(uid, "شركة الدلتا للتجارة", "300112455", "01001234567",
                                 "عميل تجريبي — بياناته وهمية")
    seller_id = db.create_client(uid, "مكتبة النيل (بيع)", "512334901", "", "عميل تجريبي بيبيع")
    db.create_client(uid, "مطعم السلطان", "421907335", "01112223344", "عميل تجريبي لسه مابعتش فواتير")
    client = db.get_client(client_id)
    seller = db.get_client(seller_id)
    from invoice_extractor.pipeline import SUPPORTED_EXTS

    files = [p for p in sorted((root / "samples" / "invoices").iterdir()) if p.suffix.lower() in SUPPORTED_EXTS]
    for path in files:
        target = seller if path.name.startswith("01_") else client
        service.add_file(target, path.name, path.read_bytes())
        if path.name.startswith("01_"):
            service.add_file(client, path.name, path.read_bytes())  # same invoice as a purchase
    _demo_history(db, uid)
    print("\n✅ اتعمل حساب تجريبي فيه عملاء وفواتير (و٥ شهور قبل كده عشان التحليلات):")
    print(f"   الإيميل: {email}\n   كلمة السر: {password}\n")


def _demo_history(db, uid: int) -> None:
    """Five earlier months of plausible, approved invoices so the analytics page has something to show."""
    import random
    from datetime import date as _date

    rng = random.Random(42)
    clients = {c["name"]: c for c in db.list_clients(uid, "2000-01")}
    suppliers = [("شركة الأهرام للتوريدات الهندسية", "488201736"), ("Nile Office Supplies LLC", "512334901"),
                 ("Giza Building Materials", "377901224"), ("مؤسسة النور للأدوات المكتبية", "615904382"),
                 ("Alex Logistics S.A.E", "205778341"), ("مطبعة القاهرة", "410229873")]
    buyers = [("سوبر ماركت الهدى", "700112233"), ("فندق النيل", "700445566"), ("شركة المقاولات المتحدة", "700778899")]
    today = _date.today()
    for back in range(5, 0, -1):
        y, m = today.year, today.month - back
        while m <= 0:
            y, m = y - 1, m + 12
        period = f"{y}-{m:02d}"
        for name, client in clients.items():
            n = rng.randint(6, 12)
            for k in range(n):
                sale = client["tax_id"] in ("512334901", "421907335") or rng.random() < 0.25
                party = rng.choice(buyers if sale else suppliers)
                sub = round(rng.uniform(800, 18000), -1)
                if client["name"] == "مطعم السلطان" and back == 2:
                    sub *= 2.6  # one unusual month, so the analytics flag it
                vat = round(sub * 0.14, 2)
                channel = rng.choices(["upload", "whatsapp", "eta_sync"], weights=[6 - back, back + 2, 3])[0]
                db.insert_invoice(client["id"], {
                    "direction": "sale" if sale else "purchase", "status": "ok", "approved": 1,
                    "source_type": "eta_api" if channel == "eta_sync" else "ai_vision", "channel": channel,
                    "original_filename": f"demo_{period}_{k}.jpg", "period": period,
                    "supplier_name": client["name"] if sale else party[0],
                    "supplier_tax_id": client["tax_id"] if sale else party[1],
                    "buyer_name": party[0] if sale else client["name"], "buyer_tax_id": party[1] if sale else client["tax_id"],
                    "invoice_number": f"D{period.replace('-', '')}{client['id']}{k:02d}",
                    "invoice_date": f"{period}-{rng.randint(1, 28):02d}", "currency": "EGP",
                    "subtotal": sub, "vat_rate": 0.14, "vat_amount": vat, "total": round(sub + vat, 2),
                })
                db.remember_party(uid, party[1], party[0])


TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"


def setup_ocr() -> None:
    import urllib.request

    from invoice_extractor.extractors import ocr

    target = ocr.PROJECT_TESSDATA
    target.mkdir(exist_ok=True)
    for lang in ("ara", "eng"):
        dest = target / f"{lang}.traineddata"
        if dest.exists() and dest.stat().st_size > 100_000:
            print(f"  ✓ {lang} موجود")
            continue
        print(f"  ↓ بينزّل {lang}…")
        try:
            urllib.request.urlretrieve(TESSDATA_URL.format(lang=lang), dest)
        except Exception as exc:  # network / proxy problems
            dest.unlink(missing_ok=True)
            print(f"  ✗ فشل تنزيل {lang}: {exc}")
    ocr.available_languages.cache_clear()
    ocr.tesseract_cmd.cache_clear()
    state = ocr.status()
    if state == "ok":
        print("✅ القراءة المجانية (OCR) جاهزة بالعربي والإنجليزي.")
    elif state == "no_arabic":
        print("⚠️  Tesseract شغال بس من غير عربي — جرّب الأمر تاني.")
    else:
        print("⚠️  برنامج Tesseract مش متسطب. سطّبه وبعدين شغّل الأمر ده تاني:")
        print("    ويندوز : winget install UB-Mannheim.TesseractOCR")
        print("    ماك    : brew install tesseract")
        print("    لينكس  : sudo apt install tesseract-ocr")


def create_user() -> None:
    from .config import load_settings
    from .db import Database

    db = Database(load_settings().database_path)
    name = input("الاسم / اسم المكتب: ").strip()
    email = input("الإيميل: ").strip()
    password = getpass.getpass("كلمة السر (٨ حروف على الأقل): ")
    if len(password) < 8 or "@" not in email or not name:
        sys.exit("بيانات غير صحيحة.")
    if db.email_exists(email):
        sys.exit("الإيميل متسجل قبل كده.")
    db.create_accountant(name, email, password)
    print("✅ اتعمل الحساب.")


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m app")
    parser.add_argument("command", nargs="?", default="run", choices=["run", "demo", "create-user", "setup-ocr"])
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = parser.parse_args()
    if args.command == "demo":
        create_demo()
    elif args.command == "create-user":
        create_user()
    elif args.command == "setup-ocr":
        setup_ocr()
    else:
        run_server(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
