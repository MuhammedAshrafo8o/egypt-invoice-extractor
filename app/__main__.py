"""python -m app                 → run the server
python -m app demo            → create a demo account with a client and sample invoices
python -m app create-user     → create an accountant account
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
    client = db.get_client(client_id)
    seller = db.get_client(seller_id)
    for path in sorted((root / "samples" / "invoices").iterdir()):
        target = seller if path.name.startswith("01_") else client
        service.add_file(target, path.name, path.read_bytes())
        if path.name.startswith("01_"):
            service.add_file(client, path.name, path.read_bytes())  # same invoice as a purchase
    print("\n✅ اتعمل حساب تجريبي فيه عميلين وفواتير:")
    print(f"   الإيميل: {email}\n   كلمة السر: {password}\n")


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
    parser.add_argument("command", nargs="?", default="run", choices=["run", "demo", "create-user"])
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes (development)")
    args = parser.parse_args()
    if args.command == "demo":
        create_demo()
    elif args.command == "create-user":
        create_user()
    else:
        run_server(args.host, args.port, args.reload)


if __name__ == "__main__":
    main()
