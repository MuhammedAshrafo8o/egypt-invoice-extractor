"""Web dashboard for accountants (Starlette + Jinja2, Arabic RTL)."""
from __future__ import annotations

import functools
import json
import logging
import secrets
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

from invoice_extractor.eta_issue import RECEIVER_TYPES, UNIT_TYPES, VAT_KIND_LABELS, check_draft
from invoice_extractor.extractors import ai_vision, ocr
from invoice_extractor.pdfium_lock import PDFIUM_LOCK

from . import charts, whatsapp
from .config import Settings, load_settings
from .db import Database
from .exports import build_workbook
from .services import (MINUTES_PER_INVOICE, DuplicateFileError, pdf_page_count, InvoiceService, IssueError, UploadError, current_period, filing_deadline,
                       invoice_question_text, reminder_text, valid_period, wa_link)

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

STATUS_LABELS = {"ok": "سليمة", "review": "مراجعة", "error": "خطأ"}
SOURCE_LABELS = {
    "pdf_text": "نص PDF", "ai_vision": "ذكاء اصطناعي", "ocr": "OCR مجاني", "eta_qr": "QR المنظومة",
    "eta_api": "المنظومة", "unknown": "غير معروف",
}
CHANNEL_LABELS = {"upload": "رفع", "whatsapp": "واتساب", "eta_sync": "المنظومة", "manual": "إدخال يدوي"}
ETA_STATE_LABELS = {"submitted": "اتبعتت — مستنية رد المنظومة", "valid": "صادرة على المنظومة",
                    "invalid": "المنظومة رفضتها", "rejected": "المنظومة رفضتها"}


def missing_on_eta_text(client_name: str, office: str, invoices: list[dict]) -> str:
    lines = [f"أهلًا {client_name}،",
             "الفواتير دي من الموردين مش متسجلة على منظومة الفاتورة الإلكترونية، "
             "فالضريبة اللي فيها ممكن متتخصمش من الإقرار:"]
    for inv in invoices[:15]:
        bits = [inv.get("supplier_name") or "مورد", f"فاتورة {inv['invoice_number']}" if inv.get("invoice_number") else "",
                inv.get("invoice_date") or "", f"{inv['total']:,.2f} ج" if inv.get("total") is not None else ""]
        lines.append("• " + " — ".join(b for b in bits if b))
    lines.append("ياريت تطلب من كل مورد يصدر الفاتورة الإلكترونية على رقمك الضريبي.")
    lines.append(f"شكرًا، {office}")
    return "\n".join(lines)
def supplier_missing_text(company, invoices: list[dict]) -> str:
    """What a company sends a supplier whose invoices are not on the e-invoice system."""
    supplier = invoices[0].get("supplier_name") or "حضرتك"
    tax = f" (رقمنا الضريبي {company['tax_id']})" if company["tax_id"] else ""
    lines = [f"السلام عليكم {supplier}،",
             f"الفواتير دي مش ظاهرة لنا على منظومة الفاتورة الإلكترونية، ومحتاجين تصدروها على {company['name']}{tax} "
             "عشان الضريبة تتخصم:"]
    for inv in invoices[:15]:
        bits = [f"فاتورة {inv['invoice_number']}" if inv.get("invoice_number") else "فاتورة",
                inv.get("invoice_date") or "", f"{inv['total']:,.2f} ج" if inv.get("total") is not None else ""]
        lines.append("• " + " — ".join(b for b in bits if b))
    lines.append("شكرًا.")
    return "\n".join(lines)


MONTHS_AR = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس",
             "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]


# Which form field each validation finding points at (to outline it on the review screen).
ISSUE_FIELDS = {
    "supplier_tax_id": ("supplier_tax_id",), "buyer_tax_id": ("buyer_tax_id",),
    "total_mismatch": ("total", "subtotal", "vat_amount"), "vat_rate": ("vat_amount",),
    "vat_missing": ("vat_amount",), "subtotal": ("subtotal",), "total_": ("total",),
    "date_": ("invoice_date",), "invoice_date": ("invoice_date",),
    "invoice_number": ("invoice_number",), "duplicate": ("invoice_number",),
    "same_tax_id": ("supplier_tax_id", "buyer_tax_id"),
}


# Problem categories for analytics: (label, test on issue code)
ISSUE_CATEGORIES = [
    ("بيانات ناقصة في الفاتورة", lambda c: c.endswith("_missing") and c != "invoice_number_missing"),
    ("الإجمالي مش مظبوط", lambda c: c == "total_mismatch"),
    ("فاتورة مكررة", lambda c: c == "duplicate"),
    ("رقم ضريبي غلط أو مشكوك فيه", lambda c: c.endswith("_format") or c.endswith("_near_known") or c.endswith("_national_id")),
    ("تاريخ غلط أو قديم", lambda c: c.startswith("date_")),
    ("نسبة ضريبة غير معتادة", lambda c: c.startswith("vat_rate") or c == "vat_missing"),
    ("صورة صعبة القراءة", lambda c: c in ("ocr_used", "ai_low_confidence", "ai_unavailable", "ai_failed", "ocr_failed")),
    ("فاتورة إلكترونية العميل مش مربوط", lambda c: c == "eta_document"),
]


def previous_period(period: str) -> str:
    y, m = map(int, period.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def pct_change(cur: float, prev: float):
    if not prev:
        return None
    return round(100 * (cur - prev) / abs(prev))


def issue_fields(issues: list) -> dict:
    """{field_name: 'error' | 'warning'} for the fields the findings refer to."""
    marked: dict = {}
    for issue in issues or []:
        code = issue.get("code", "")
        for prefix, fields in ISSUE_FIELDS.items():
            if code.startswith(prefix):
                for f in fields:
                    if marked.get(f) != "error":
                        marked[f] = issue.get("level", "warning")
    return marked


def money(value) -> str:
    if value is None or value == "":
        return "—"
    return f"{float(value):,.2f}"


def dmy(value) -> str:
    """2026-10-15 -> 15/10/2026 (how dates are written on Egyptian invoices)."""
    if not value:
        return "—"
    try:
        y, m, d = str(value)[:10].split("-")
        return f"{d}/{m}/{y}"
    except ValueError:
        return str(value)


def amount_input(value) -> str:
    return "" if value is None or value == "" else f"{float(value):.2f}"


def period_label(period: str) -> str:
    try:
        year, month = period.split("-")
        return f"{MONTHS_AR[int(month) - 1]} {year}"
    except (ValueError, IndexError):
        return period


def recent_periods(extra: list[str] | None = None, count: int = 12) -> list[str]:
    today = date.today()
    periods = []
    y, m = today.year, today.month
    for _ in range(count):
        periods.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    for p in extra or []:
        if p not in periods:
            periods.append(p)
    return sorted(set(periods), reverse=True)


def create_app(settings: Settings | None = None, eta_client_factory=None,
               whatsapp_client: whatsapp.WhatsAppClient | None = None) -> Starlette:
    settings = settings or load_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    db = Database(settings.database_path)
    service = InvoiceService(db, settings, **({"eta_client_factory": eta_client_factory}
                                               if eta_client_factory else {}))
    wa = whatsapp_client or whatsapp.WhatsAppClient(settings)

    templates = Jinja2Templates(directory=str(HERE / "templates"))
    env = templates.env
    env.filters["money"] = money
    env.filters["period_label"] = period_label
    env.filters["dmy"] = dmy
    env.filters["amount_input"] = amount_input
    env.globals.update(STATUS_LABELS=STATUS_LABELS, SOURCE_LABELS=SOURCE_LABELS,
                       CHANNEL_LABELS=CHANNEL_LABELS, ETA_STATE_LABELS=ETA_STATE_LABELS,
                       VAT_KIND_LABELS=VAT_KIND_LABELS, UNIT_TYPES=UNIT_TYPES, RECEIVER_TYPES=RECEIVER_TYPES)

    # ---------------------------------------------------------------- helpers
    def reader_status() -> dict:
        """Which engine reads photos / scanned PDFs right now (shown in the top bar)."""
        if settings.use_ai and ai_vision.is_available():
            return {"kind": "ai", "label": f"القراءة: {ai_vision.provider_label()}"}
        if settings.use_ocr and ocr.is_available():
            label = "القراءة: OCR مجاني" if "ara" in ocr.available_languages() else "OCR بدون عربي"
            return {"kind": "ocr", "label": label}
        return {"kind": "off", "label": "قراءة الصور مقفولة"}

    def render(request: Request, template: str, status_code: int = 200, **ctx):
        session = request.session
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        flashes = session.pop("flashes", [])
        user = db.get_accountant(session["uid"]) if session.get("uid") else None
        return templates.TemplateResponse(request, template, {
            "user": user, "csrf": session["csrf"], "flashes": flashes, "company": is_company(user),
            **({"nav": "home"} if is_company(user) and ctx.get("nav") == "clients" else {}),
            "settings": settings, "reader": reader_status(),
            "pending_total": db.pending_count(user["id"]) if user else 0,
            **{k: v for k, v in ctx.items() if not (k == "nav" and is_company(user) and v == "clients")},
        }, status_code=status_code)

    def flash(request: Request, message: str, kind: str = "info") -> None:
        request.session.setdefault("flashes", []).append({"kind": kind, "text": message})

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    async def form_with_csrf(request: Request):
        form = await request.form()
        if not secrets.compare_digest(str(form.get("csrf", "")), request.session.get("csrf", "")):
            raise PermissionError("CSRF")
        return form

    def login_required(handler):
        @functools.wraps(handler)
        async def wrapper(request: Request):
            uid = request.session.get("uid")
            user = db.get_accountant(uid) if uid else None
            if not user:
                return redirect("/login?" + urlencode({"next": request.url.path}))
            request.state.user = user
            try:
                return await handler(request)
            except PermissionError:
                flash(request, "انتهت صلاحية الصفحة، جرّب تاني.", "error")
                return redirect(request.headers.get("referer") or "/")
        return wrapper

    def get_client_or_404(request: Request, client_id: int):
        client = db.get_client(client_id, request.state.user["id"])
        if not client:
            raise LookupError
        return client

    def is_company(user) -> bool:
        return bool(user) and (user["kind"] or "office") == "company"

    def company_record(user):
        """The company's own record (one per company account), created on first use."""
        record = db.company_of(user["id"])
        if record is None:
            db.create_client(user["id"], user["name"], "", "", "")
            record = db.company_of(user["id"])
        return record

    def settings_url(request: Request, client, anchor: str = "") -> str:
        base = "/settings" if is_company(request.state.user) else f"/clients/{client['id']}"
        return base + (f"#{anchor}" if anchor else "")

    def get_invoice_or_404(request: Request, invoice_id: int):
        invoice = db.get_invoice(invoice_id, request.state.user["id"])
        if not invoice:
            raise LookupError
        return invoice

    # ------------------------------------------------------------------ auth
    async def login(request: Request):
        if request.method == "POST":
            form = await request.form()
            user = db.authenticate(form.get("email", ""), form.get("password", ""))
            if user:
                request.session.clear()
                request.session["uid"] = user["id"]
                nxt = request.query_params.get("next", "/")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
            flash(request, "الإيميل أو كلمة السر غلط.", "error")
            return render(request, "login.html", email=form.get("email", ""), status_code=400)
        return render(request, "login.html")

    async def register(request: Request):
        if not settings.allow_signup:
            flash(request, "التسجيل مقفول. كلّم مسؤول النظام.", "error")
            return redirect("/login")
        if request.method == "POST":
            form = await request.form()
            name, email = form.get("name", "").strip(), form.get("email", "").strip()
            password = form.get("password", "")
            kind = "office" if form.get("kind") == "office" else "company"
            tax_id = "".join(ch for ch in form.get("tax_id", "") if ch.isdigit())
            errors = []
            if not name:
                errors.append("اكتب اسم الشركة." if kind == "company" else "اكتب اسمك أو اسم المكتب.")
            if "@" not in email:
                errors.append("الإيميل مش صحيح.")
            elif db.email_exists(email):
                errors.append("الإيميل ده متسجل قبل كده.")
            if len(password) < 8:
                errors.append("كلمة السر لازم تكون ٨ حروف على الأقل.")
            if errors:
                for e in errors:
                    flash(request, e, "error")
                return render(request, "register.html", name=name, email=email, kind=kind, tax_id=tax_id,
                              status_code=400)
            uid = db.create_accountant(name, email, password, kind)
            if kind == "company":
                db.create_client(uid, name, tax_id, "", "")
            request.session.clear()
            request.session["uid"] = uid
            if kind == "company":
                flash(request, "أهلًا بيك! كمّل بيانات الشركة والربط بالمنظومة من الإعدادات، وبعدين ارفع أول فواتير.", "success")
                return redirect("/settings")
            flash(request, "أهلًا بيك! ابدأ بإضافة أول عميل.", "success")
            return redirect("/")
        return render(request, "register.html")

    async def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    # ------------------------------------------------------------- dashboard
    @login_required
    async def dashboard(request: Request):
        if is_company(request.state.user):
            return client_page(request, company_record(request.state.user), nav="home")
        period = valid_period(request.query_params.get("period"))
        clients = db.list_clients(request.state.user["id"], period)
        for c in clients:
            c["pending"] = (c["error_count"] or 0) + (c["review_count"] or 0)
            c["done"] = (c["invoice_count"] or 0) - c["pending"]
            c["net_vat"] = (c["output_vat"] or 0) - (c["input_vat"] or 0)
            c["ready"] = bool(c["invoice_count"]) and c["pending"] == 0
        clients.sort(key=lambda c: (c["ready"], -c["pending"], c["name"]))
        totals = {
            "clients": len(clients),
            "invoices": sum(c["invoice_count"] or 0 for c in clients),
            "errors": sum(c["error_count"] or 0 for c in clients),
            "reviews": sum(c["review_count"] or 0 for c in clients),
            "ready": sum(1 for c in clients if c["ready"]),
            "net_vat": round(sum(c["net_vat"] for c in clients), 2),
        }
        deadline = filing_deadline(settings.vat_deadline_day)
        stats = db.work_stats(request.state.user["id"], period)
        minutes = stats["auto_read"] * MINUTES_PER_INVOICE
        office = request.state.user["name"]
        days_left = deadline["days_left"] if deadline["period"] == period else None
        for c in clients:
            c["remind_url"] = wa_link(c["whatsapp_phone"], reminder_text(
                c["name"], office, period_label(period), days_left, settings.whatsapp_display_number))
        today = date.today()
        for c in clients:
            last = c.get("last_received")
            c["days_since"] = (today - date.fromisoformat(last[:10])).days if last else None
        quiet = [c for c in clients if not c["invoice_count"]]
        return render(request, "dashboard.html", clients=clients, period=period, nav="home",
                      periods=recent_periods(), totals=totals, deadline=deadline, quiet=quiet,
                      stats=stats, hours_saved=round(minutes / 60, 1))

    def search_params(request: Request) -> dict:
        """Read invoice-search filters from the query string (shared by the page, CSV export and API)."""
        qp = request.query_params

        def num(name):
            try:
                return float(str(qp.get(name)).replace(",", "")) if qp.get(name) not in (None, "") else None
            except ValueError:
                return None

        def day(name):
            value = qp.get(name) or ""
            return value if len(value) == 10 and value[4] == "-" and value[7] == "-" else None

        client_id = qp.get("client")
        period = qp.get("period")
        return {
            "q": (qp.get("q") or "").strip()[:100],
            "client_id": int(client_id) if client_id and client_id.isdigit() else None,
            "period": period if period and valid_period(period) == period else None,
            "status": qp.get("status") or None,
            "direction": qp.get("direction") or None,
            "channel": qp.get("channel") or None,
            "date_from": day("from"), "date_to": day("to"),
            "amount_min": num("min"), "amount_max": num("max"),
            "sort": qp.get("sort") or "date_desc",
        }

    @login_required
    async def invoices_page(request: Request):
        params = search_params(request)
        page = max(1, int(request.query_params.get("page") or 1)) if str(request.query_params.get("page") or "1").isdigit() else 1
        per_page = 50
        rows, totals = db.search_invoices(request.state.user["id"], limit=per_page, offset=(page - 1) * per_page, **params)
        clients = db.list_clients(request.state.user["id"], current_period())
        active = {k: v for k, v in request.query_params.items() if v and k not in ("page", "sort")}
        return render(request, "invoices.html", nav="invoices", rows=rows, totals=totals, params=params,
                      clients=clients, periods=recent_periods(), page=page, per_page=per_page,
                      pages=max(1, -(-totals["n"] // per_page)), active=active,
                      query_string=lambda **kw: urlencode({**{k: v for k, v in request.query_params.items() if v},
                                                           **{k: v for k, v in kw.items()}}))

    @login_required
    async def invoices_csv(request: Request):
        import csv
        import io as _io

        rows, _ = db.search_invoices(request.state.user["id"], limit=10000, **search_params(request))
        buf = _io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["العميل", "التاريخ", "رقم الفاتورة", "النوع", "المورد", "الرقم الضريبي للمورد", "المشتري",
                         "الرقم الضريبي للمشتري", "قبل الضريبة", "الضريبة", "الإجمالي", "العملة", "الحالة", "المصدر"])
        for r in rows:
            writer.writerow([r["client_name"], dmy(r["invoice_date"]), r["invoice_number"] or "",
                             "مبيعات" if r["direction"] == "sale" else "مشتريات", r["supplier_name"] or "",
                             r["supplier_tax_id"] or "", r["buyer_name"] or "", r["buyer_tax_id"] or "",
                             r["subtotal"] if r["subtotal"] is not None else "",
                             r["vat_amount"] if r["vat_amount"] is not None else "",
                             r["total"] if r["total"] is not None else "", r["currency"] or "",
                             "معتمدة" if r["approved"] else STATUS_LABELS.get(r["status"], r["status"]),
                             CHANNEL_LABELS.get(r["channel"], r["channel"])])
        return Response(("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": 'attachment; filename="invoices.csv"'})

    @login_required
    async def api_search(request: Request):
        """Quick results for the command palette (Ctrl+K)."""
        q = (request.query_params.get("q") or "").strip()[:100]
        if not q:
            return JSONResponse({"clients": [], "invoices": []})
        uid = request.state.user["id"]
        invoices, _ = db.search_invoices(uid, q=q, limit=8, sort="recent")
        return JSONResponse({
            "clients": [{"id": c["id"], "name": c["name"], "tax_id": c["tax_id"]} for c in db.search_clients(uid, q)],
            "invoices": [{"id": i["id"], "title": ((i["buyer_name"] if i["direction"] == "sale" else i["supplier_name"])
                                                  or i["original_filename"] or "فاتورة")
                          + (f" — {i['invoice_number']}" if i["invoice_number"] else ""),
                          "client": i["client_name"], "total": money(i["total"]), "date": dmy(i["invoice_date"]),
                          "status": "approved" if i["approved"] else i["status"]} for i in invoices],
        })

    @login_required
    async def company_settings(request: Request):
        if not is_company(request.state.user):
            return redirect("/")
        client = company_record(request.state.user)
        return render(request, "settings.html", client=client, profile=service.profile(client), nav="settings",
                      eta_ready=bool(client["eta_client_id"] and client["eta_client_secret"]))

    @login_required
    async def client_eta_test(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        await form_with_csrf(request)
        ok, message = await run_in_threadpool(service.test_eta, client)
        flash(request, message, "success" if ok else "error")
        return redirect(settings_url(request, client, "settings"))

    @login_required
    async def signer_page(request: Request):
        return render(request, "signer.html", nav="")

    @login_required
    async def analytics(request: Request):
        uid = request.state.user["id"]
        period = valid_period(request.query_params.get("period"))
        if is_company(request.state.user):
            from .reports import company_report
            company = company_record(request.state.user)
            report = await run_in_threadpool(company_report, db, company, period)
            labels = [period_label(m).split(" ")[0] for m in report["periods"]]
            trade_chart = charts.bar_chart(labels, [
                {"name": "المبيعات", "values": [report["by_month"][m]["sales"] for m in report["periods"]]},
                {"name": "المشتريات", "values": [report["by_month"][m]["purchases"] for m in report["periods"]]},
            ], aria="المبيعات والمشتريات لكل شهر (قبل الضريبة)")
            return render(request, "reports.html", nav="analytics", client=company, period=period,
                          periods=recent_periods(db.periods(company["id"])), report=report,
                          trade_chart=trade_chart, trade_legend=charts.legend(["المبيعات", "المشتريات"]))
        months = [period]
        for _ in range(5):
            months.insert(0, previous_period(months[0]))
        prev = months[-2]
        series = db.monthly_series(uid, months)
        cur, before = series[period], series[prev]
        net = lambda s: round(s["output_vat"] - s["input_vat"], 2)  # noqa: E731

        labels = [period_label(m).split(" ")[0] for m in months]
        vat_chart = charts.bar_chart(labels, [
            {"name": "ضريبة المخرجات", "values": [series[m]["output_vat"] for m in months]},
            {"name": "ضريبة المدخلات", "values": [series[m]["input_vat"] for m in months]},
        ], aria="ضريبة المخرجات والمدخلات لكل شهر")
        channel_chart = charts.bar_chart(labels, [
            {"name": "رفع ملفات", "values": [series[m]["upload"] for m in months]},
            {"name": "واتساب", "values": [series[m]["whatsapp"] for m in months]},
            {"name": "المنظومة", "values": [series[m]["eta_sync"] for m in months]},
        ], stacked=True, money=False, aria="عدد الفواتير لكل شهر حسب المصدر")

        # per client, this month vs last month
        now_rows = {c["id"]: c for c in db.list_clients(uid, period)}
        prev_rows = {c["id"]: c for c in db.list_clients(uid, prev)}
        clients = []
        for cid, c in now_rows.items():
            p = prev_rows.get(cid, {})
            c_net = (c["output_vat"] or 0) - (c["input_vat"] or 0)
            p_net = (p.get("output_vat") or 0) - (p.get("input_vat") or 0)
            change = pct_change(c_net, p_net)
            clients.append({"id": cid, "name": c["name"], "invoices": c["invoice_count"] or 0,
                            "prev_invoices": p.get("invoice_count") or 0, "net": c_net, "prev_net": p_net,
                            "change": change, "pending": (c["error_count"] or 0) + (c["review_count"] or 0),
                            "flag": change is not None and abs(change) >= 50 and abs(c_net - p_net) >= 500})
        clients.sort(key=lambda c: -abs(c["net"]))

        # what goes wrong most
        counts = {label: 0 for label, _ in ISSUE_CATEGORIES}
        for issues in db.period_issues(uid, period):
            codes = {i.get("code", "") for i in issues}
            for label, test in ISSUE_CATEGORIES:
                if any(test(c) for c in codes):
                    counts[label] += 1
        problems = sorted(((k, v) for k, v in counts.items() if v), key=lambda kv: -kv[1])

        suppliers = db.top_suppliers(uid, period)
        stats = db.work_stats(uid, period)

        # plain-language observations
        insights = []
        if cur["invoices"]:
            share_wa = round(100 * cur["whatsapp"] / cur["invoices"])
            share_eta = round(100 * cur["eta_sync"] / cur["invoices"])
            if share_wa:
                insights.append(f"{share_wa}٪ من فواتير الشهر وصلت على الواتساب من غير ما حد يطاردها.")
            if share_eta:
                insights.append(f"{share_eta}٪ اتسحبت من المنظومة جاهزة ومعتمدة.")
            fix_rate = round(100 * cur["errors"] / cur["invoices"])
            insights.append(f"{fix_rate}٪ من الفواتير احتاجت تصليح." + (
                f" أكتر سبب: {problems[0][0]}." if problems else ""))
        for c in [c for c in clients if c["flag"]][:3]:
            direction = "زادت" if c["net"] > c["prev_net"] else "قلّت"
            insights.append(f"صافي ضريبة {c['name']} {direction} {abs(c['change'])}٪ عن {period_label(prev)}، "
                            f"يستاهل نظرة قبل الإقرار.")
        quiet = [c for c in clients if c["prev_invoices"] and not c["invoices"]]
        if quiet:
            names = "، ".join(c["name"] for c in quiet[:3])
            insights.append(f"{names} بعتوا فواتير {period_label(prev)} بس لسه مابعتوش {period_label(period)}.")

        return render(request, "analytics.html", nav="analytics", period=period, prev=prev,
                      periods=recent_periods(), months=months, series=series, cur=cur, before=before,
                      net_now=net(cur), net_prev=net(before), net_change=pct_change(net(cur), net(before)),
                      inv_change=pct_change(cur["invoices"], before["invoices"]),
                      vat_chart=vat_chart, channel_chart=channel_chart,
                      vat_legend=charts.legend(["ضريبة المخرجات", "ضريبة المدخلات"]),
                      channel_legend=charts.legend(["رفع ملفات", "واتساب", "المنظومة"]),
                      clients=clients, problems=problems, problem_max=max([v for _, v in problems] or [1]),
                      suppliers=suppliers, supplier_max=max([s["vat"] for s in suppliers] or [1]),
                      hours_saved=round(stats["auto_read"] * MINUTES_PER_INVOICE / 60, 1), insights=insights)

    @login_required
    async def review_queue(request: Request):
        queue = db.pending_invoices(request.state.user["id"])
        groups: dict = {}
        for inv in queue:
            groups.setdefault((inv["client_id"], inv["client_name"]), []).append(inv)
        return render(request, "review.html", nav="review", queue=queue, groups=groups)

    @login_required
    async def client_create(request: Request):
        form = await form_with_csrf(request)
        if is_company(request.state.user):
            return redirect("/")
        name = form.get("name", "").strip()
        phone = form.get("whatsapp_phone", "")
        if not name:
            flash(request, "اكتب اسم العميل.", "error")
            return redirect("/")
        if db.phone_taken(phone):
            flash(request, "رقم الواتساب ده متسجل لعميل تاني.", "error")
            return redirect("/")
        cid = db.create_client(request.state.user["id"], name, form.get("tax_id", ""), phone,
                               form.get("notes", ""))
        flash(request, f"اتضاف العميل {name}.", "success")
        return redirect(f"/clients/{cid}")

    # ---------------------------------------------------------------- client
    @login_required
    async def client_detail(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        if is_company(request.state.user):     # one company, one page: its home
            return redirect("/" + (f"?{request.url.query}" if request.url.query else ""))
        return client_page(request, client)

    def client_page(request: Request, client, nav: str = "clients"):
        period = valid_period(request.query_params.get("period"))
        status = request.query_params.get("status") or None
        direction = request.query_params.get("direction") or None
        q = (request.query_params.get("q") or "").strip()[:100]
        if q:
            invoices, _ = db.search_invoices(request.state.user["id"], q=q, client_id=client["id"], period=period,
                                             status=status, direction=direction, limit=500)
        else:
            invoices = db.list_invoices(client["id"], period, status, direction)
        deadline = filing_deadline(settings.vat_deadline_day)
        remind_url = wa_link(client["whatsapp_phone"], reminder_text(
            client["name"], request.state.user["name"], period_label(period),
            deadline["days_left"] if deadline["period"] == period else None, settings.whatsapp_display_number))
        missing = service.missing_on_eta(client["id"], period)
        if is_company(request.state.user):
            remind_url = None           # nobody to remind: the company uploads its own invoices
            missing_url = None
            for inv in missing:         # one message per supplier, to send from the company's WhatsApp
                same = [i for i in missing if (i.get("supplier_tax_id") or i.get("supplier_name")) ==
                        (inv.get("supplier_tax_id") or inv.get("supplier_name"))]
                inv["supplier_url"] = wa_link(None, supplier_missing_text(client, same), pick=True)
        else:
            missing_url = wa_link(client["whatsapp_phone"], missing_on_eta_text(
                client["name"], request.state.user["name"], missing)) if missing else None
        return render(request, "client.html", client=client, invoices=invoices, period=period, nav=nav,
                      remind_url=remind_url, q=q, missing=missing, missing_url=missing_url,
                      progress=db.batch_progress(client["id"]),
                      profile=service.profile(client), can_issue=service.can_issue(client),
                      periods=recent_periods(db.periods(client["id"])), status=status, direction=direction,
                      summary=service.summary(client["id"], period))

    @login_required
    async def client_update(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        if form.get("action") == "delete":
            if form.get("confirm_name", "").strip() != client["name"]:
                flash(request, "اكتب اسم العميل بالظبط عشان تأكد المسح.", "error")
                return redirect(settings_url(request, client, "settings"))
            if is_company(request.state.user):
                flash(request, "حساب الشركة مينفعش يتمسح من هنا.", "error")
                return redirect("/settings")
            db.delete_client(client["id"])
            flash(request, f"اتمسح العميل {client['name']} وكل فواتيره.", "success")
            return redirect("/")
        phone = form.get("whatsapp_phone", "")
        if db.phone_taken(phone, except_client_id=client["id"]):
            flash(request, "رقم الواتساب ده متسجل لعميل تاني.", "error")
            return redirect(settings_url(request, client, "settings"))
        fields = {k: form.get(k, "") for k in ("name", "tax_id", "whatsapp_phone", "notes", "eta_client_id")}
        if form.get("eta_client_secret"):  # keep the stored secret unless a new one is typed
            fields["eta_client_secret"] = service.box.encrypt(form.get("eta_client_secret").strip())
        if form.get("clear_eta"):
            fields["eta_client_id"] = ""
            fields["eta_client_secret"] = ""
        if not fields["name"].strip():
            flash(request, "الاسم مطلوب.", "error")
        else:
            db.update_client(client["id"], **fields)
            flash(request, "اتحفظت البيانات.", "success")
        if is_company(request.state.user):
            return redirect("/settings")
        return redirect(f"/clients/{client['id']}")

    @login_required
    async def client_upload(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
        viewed_period = valid_period(form.get("period"))
        back = f"/clients/{client['id']}?period={viewed_period}"
        if not files:
            flash(request, "اختار ملف واحد على الأقل.", "error")
            return redirect(back)
        limit = settings.max_files_per_upload
        if len(files) > limit:
            flash(request, f"اخترت {len(files)} ملف، والحد {limit} ملف في المرة. ارفعهم على كذا دفعة.", "error")
            return redirect(back)
        contents = []
        for upload in files:
            contents.append((upload.filename, await upload.read()))
            await upload.close()
        split = bool(form.get("split_pages"))
        if form.get("batch"):
            # The browser uploader sends one file per request (serverless hosts cap request size)
            # and shows the results itself.
            result = await run_in_threadpool(service.queue_files, client, contents, "upload", split,
                                             None, None, form.get("batch"))
            return JSONResponse({k: result[k] for k in ("queued", "skipped", "failed", "notes")})
        if len(contents) == 1 and not (split and pdf_page_count(*contents[0]) > 1):
            # One invoice: read it now and open it, so its fields are on screen.
            filename, content = contents[0]
            try:
                inv_id = await run_in_threadpool(service.add_file, client, filename, content, "upload")
            except DuplicateFileError as exc:
                flash(request, str(exc), "info")
                return redirect(back)
            except UploadError as exc:
                flash(request, f"\u2068{filename}\u2069: {exc}", "error")
                return redirect(back)
            flash(request, "اتقرت الفاتورة — راجع البيانات، صلّح لو فيه غلط، واعتمدها.", "success")
            pages = pdf_page_count(filename, content)
            if pages > 1:
                flash(request, f"الملف ده فيه {pages} صفحات واتسجل فاتورة واحدة. لو كل صفحة فاتورة لوحدها، امسحه "
                               "وارفعه تاني مع اختيار «كل صفحة في الـ PDF فاتورة».", "info")
            return redirect(f"/invoices/{inv_id}")
        result = await run_in_threadpool(service.queue_files, client, contents, "upload", split)
        for msg in result["notes"]:
            flash(request, msg, "info")
        if result["queued"]:
            flash(request, f"اترفع {result['queued']} {'فاتورة' if split else 'ملف'}، وكل {'واحدة' if split else 'ملف'} هيتسجل فاتورة لوحده. بيتقروا دلوقتي في الخلفية — "
                           "تقدر تكمّل شغلك، والفواتير بتظهر هنا أول بأول.", "success")
        for msg in result["skipped"][:10]:
            flash(request, msg, "info")
        if len(result["skipped"]) > 10:
            flash(request, f"و{len(result['skipped']) - 10} ملف تاني متكرر اتخطّوا.", "info")
        for msg in result["failed"][:10]:
            flash(request, msg, "error")
        return redirect(back)

    async def run_jobs(request: Request):
        """Called every few seconds by the database scheduler (pg_cron) on serverless hosts."""
        auth = request.headers.get("authorization", "")
        if not settings.cron_secret or not secrets.compare_digest(auth, f"Bearer {settings.cron_secret}"):
            return PlainTextResponse("forbidden", status_code=403)
        done = await run_in_threadpool(service.run_jobs_for, 40)
        return JSONResponse({"done": done})

    @login_required
    async def kick_jobs(request: Request):
        """The page that just uploaded files starts reading them right away (no wait for the scheduler)."""
        done = await run_in_threadpool(service.run_jobs_for, 40)
        return JSONResponse({"done": done})

    @login_required
    async def client_progress(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        return JSONResponse(db.batch_progress(client["id"]) or {"running": False, "total": 0, "done": 0})

    @login_required
    async def client_approve_bulk(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        ids = [int(x) for x in form.getlist("ids") if str(x).isdigit()]
        done = service.approve_many(client, ids)
        skipped = len(ids) - done
        if done:
            flash(request, f"اتعتمدت {done} فاتورة.", "success")
        if skipped:
            flash(request, f"{skipped} فاتورة ماتعتمدتش لأن فيها أخطاء لازم تتصلح الأول.", "error")
        if not ids:
            flash(request, "اختار الفواتير اللي عايز تعتمدها.", "info")
        return redirect(f"/clients/{client['id']}?period={valid_period(form.get('period'))}")

    @login_required
    async def client_report(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        period = valid_period(request.query_params.get("period"))
        invoices = db.list_invoices(client["id"], period)
        # Only what the business owner can act on: invoices left out of the totals because the document
        # itself is unclear or inconsistent. Duplicates and internal review notes stay with the accountant.
        open_items = [i for i in invoices if i["status"] == "error" and not i["approved"]
                      and not any(x.get("code") == "duplicate" for x in i["issues"])]
        return render(request, "report.html", client=client, period=period, invoices=invoices,
                      open_items=open_items, summary=service.summary(client["id"], period),
                      deadline=filing_deadline(settings.vat_deadline_day), today=date.today())

    @login_required
    async def client_export(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        period = valid_period(request.query_params.get("period"))
        invoices = db.list_invoices(client["id"], period)
        content = build_workbook(dict(client), period, invoices, service.summary(client["id"], period))
        filename = f"vat_{client['id']}_{period}.xlsx"
        return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @login_required
    async def client_eta_sync(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        period = valid_period(form.get("period"))
        result = await run_in_threadpool(service.sync_from_eta, client, period)
        if result.imported or result.skipped or result.matched:
            msg = f"اتسحب {result.imported} فاتورة جديدة من المنظومة (واتخطّى {result.skipped} موجودين أو ملغيين)."
            if result.matched:
                msg += f" واتربطت {result.matched} فاتورة مرفوعة بنسختها الإلكترونية."
            flash(request, msg, "success")
        elif not result.errors and not result.missing:
            flash(request, "مفيش فواتير في المنظومة للشهر ده.", "info")
        if result.missing and not result.errors:
            flash(request, f"{result.missing} فاتورة مشتريات مش موجودة على المنظومة — ضريبتها "
                           f"{money(result.missing_vat)} جنيه ممكن متتخصمش. شوفها تحت.", "error")
        for error in result.errors[:5]:
            flash(request, error, "error")
        return redirect(f"/clients/{client['id']}?period={period}")

    @login_required
    async def client_einvoice_settings(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        service.save_profile(client, form)
        flash(request, "اتحفظت إعدادات الفاتورة الإلكترونية.", "success")
        nxt = form.get("next") or ""
        return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else settings_url(request, client, "einvoice"))

    @login_required
    async def client_new_sale(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        await form_with_csrf(request)
        inv_id = service.new_sale(client)
        return redirect(f"/invoices/{inv_id}/issue")

    # ------------------------------------------------------ e-invoice issuing
    def flash_issue_result(request, invoice):
        state = invoice.get("eta_state")
        if state == "valid":
            flash(request, "الفاتورة اتصدرت على المنظومة واتقبلت.", "success")
        elif state == "submitted":
            flash(request, "الفاتورة اتبعتت. المنظومة بتراجعها — حدّث الحالة بعد دقيقة.", "info")
        else:
            flash(request, "المنظومة رفضت الفاتورة — الأسباب مكتوبة تحت. صلّحها وابعت تاني.", "error")

    @login_required
    async def invoice_issue(request: Request):
        invoice = get_invoice_or_404(request, request.path_params["invoice_id"])
        client = db.get_client(invoice["client_id"])
        if invoice["direction"] != "sale":
            flash(request, "الفاتورة الإلكترونية للمشتريات بيصدرها المورد. تقدر تتأكد إنها موجودة على المنظومة "
                           "بزرار «سحب من المنظومة» في صفحة العميل.", "info")
            return redirect(f"/invoices/{invoice['id']}")
        if request.method == "POST":
            form = await form_with_csrf(request)
            action = form.get("action", "save")
            try:
                if action == "upload":
                    upload = form.get("file")
                    if not getattr(upload, "filename", ""):
                        flash(request, "اختار صورة الفاتورة أو ملف PDF.", "error")
                        return redirect(f"/invoices/{invoice['id']}/issue")
                    content = await upload.read()
                    notes = await run_in_threadpool(service.fill_sale_from_upload, client, invoice,
                                                    upload.filename, content)
                    flash(request, "اتقرت الفاتورة واتملت البيانات — راجعها قبل ما تبعت.", "success")
                    for note in notes:
                        flash(request, note, "info")
                    return redirect(f"/invoices/{invoice['id']}/issue")
                if action == "refresh":
                    invoice = await run_in_threadpool(service.refresh_issue, client, invoice)
                    flash(request, ETA_STATE_LABELS.get(invoice.get("eta_state") or "", "اتحدثت الحالة."),
                          "success" if invoice.get("eta_state") == "valid" else "info")
                    return redirect(f"/invoices/{invoice['id']}/issue")
                if action == "finish":   # the signing program signed: assemble and submit
                    invoice = await run_in_threadpool(service.finish_remote, client, db.get_invoice(invoice["id"]),
                                                      form.get("ticket", ""), form.get("signature", ""))
                    flash_issue_result(request, invoice)
                    return JSONResponse({"ok": True, "redirect": f"/invoices/{invoice['id']}/issue"})
                draft = service.draft_from_form(form)
                service.save_draft(client, invoice, draft)
                if action == "prepare":  # for the signing program: what the token must sign
                    prepared = await run_in_threadpool(service.prepare_remote, client, db.get_invoice(invoice["id"]),
                                                       form.get("cert", ""))
                    return JSONResponse({"ok": True, **prepared})
                if action == "submit":
                    invoice = await run_in_threadpool(service.issue, client, db.get_invoice(invoice["id"]),
                                                      form.get("secret", ""))
                    flash_issue_result(request, invoice)
                else:
                    flash(request, "اتحفظت الفاتورة.", "success")
            except IssueError as exc:
                if action in ("prepare", "finish"):
                    return JSONResponse({"ok": False, "errors": exc.messages[:8]})
                for msg in exc.messages[:8]:
                    flash(request, msg, "error")
            return redirect(f"/invoices/{invoice['id']}/issue")
        draft = service.draft_for(client, invoice)
        profile = service.profile(client)
        problems = check_draft(draft, service.issuer(client), profile)
        line_amounts = [l.amounts() for l in draft.lines]
        return render(request, "issue.html", invoice=invoice, client=client, draft=draft, profile=profile,
                      issuer=service.issuer(client), problems=problems, totals=draft.totals(),
                      line_amounts=line_amounts, can_issue=service.can_issue(client), nav="clients",
                      signing_program=service.uses_signing_program(client),
                      locked=invoice.get("eta_state") in ("submitted", "valid"),
                      has_file=bool(invoice.get("stored_path")))

    # --------------------------------------------------------------- invoice
    @login_required
    async def invoice_detail(request: Request):
        invoice = get_invoice_or_404(request, request.path_params["invoice_id"])
        client = db.get_client(invoice["client_id"])
        queue_mode = request.query_params.get("queue") == "1"
        if queue_mode:
            all_ids = [i["id"] for i in db.pending_invoices(request.state.user["id"])]
        else:
            all_ids = [i["id"] for i in db.list_invoices(client["id"], invoice["period"], "pending")]
        # next = the first pending invoice after this one (wrapping around)
        if invoice["id"] in all_ids:
            pos = all_ids.index(invoice["id"])
            ids = all_ids[pos + 1:] + all_ids[:pos]
        else:
            pos, ids = None, all_ids
        queue_info = {"pos": (pos + 1) if pos is not None else None, "total": len(all_ids)} if queue_mode else None
        is_pdf = service.files.suffix(invoice.get("stored_path")) == ".pdf"
        page_count = 0
        if is_pdf:
            try:
                import pypdfium2 as pdfium
                content = await run_in_threadpool(service.files.read, invoice["stored_path"])
                with PDFIUM_LOCK:
                    pdf = pdfium.PdfDocument(content)
                    page_count = min(len(pdf), 5)
                    pdf.close()
            except Exception:
                page_count = 1
        first = next((i for i in invoice["issues"] if i.get("level") == "error"), None) or \
            next(iter(invoice["issues"]), None)
        problem = (first or {}).get("message_ar") or "محتاج أتأكد من بيانات الفاتورة دي."
        ask_url = wa_link(client["whatsapp_phone"], invoice_question_text(
            client["name"], request.state.user["name"], invoice, problem))
        return render(request, "invoice.html", invoice=invoice, client=client, next_id=ids[0] if ids else None,
                      ask_url=ask_url,
                      is_pdf=is_pdf, page_count=page_count, has_file=bool(invoice.get("stored_path")),
                      periods=recent_periods([invoice["period"]]), queue=queue_info,
                      marked=issue_fields(invoice["issues"]), nav="review" if queue_mode else "clients")

    @login_required
    async def invoice_update(request: Request):
        invoice = get_invoice_or_404(request, request.path_params["invoice_id"])
        client = db.get_client(invoice["client_id"])
        form = await form_with_csrf(request)
        action = form.get("action", "save")
        back = f"/clients/{client['id']}?period={invoice['period']}"
        if action == "delete":
            db.delete_invoice(invoice["id"])
            service.files.delete(invoice.get("stored_path"))
            flash(request, "اتمسحت الفاتورة.", "success")
            return redirect("/review" if form.get("queue") == "1" else back)
        if action == "reprocess":
            try:
                await run_in_threadpool(service.reprocess, client, invoice)
                flash(request, "اتقرت الفاتورة من جديد.", "success")
            except UploadError as exc:
                flash(request, str(exc), "error")
            return redirect(f"/invoices/{invoice['id']}")
        if action == "approve":
            if service.approve(invoice):
                flash(request, "اتعتمدت الفاتورة.", "success")
            else:
                flash(request, "صلّح الأخطاء الأول قبل الاعتماد.", "error")
            return redirect(f"/invoices/{invoice['id']}")
        if action == "unapprove":
            db.update_invoice(invoice["id"], {"approved": 0})
            return redirect(f"/invoices/{invoice['id']}")

        q = "?queue=1" if form.get("queue") == "1" else ""
        approve = action in ("save_approve", "save_next")
        service.save_edit(client, invoice, dict(form), approve=approve)
        updated = db.get_invoice(invoice["id"])
        if approve and not updated["approved"]:
            flash(request, "اتحفظت التعديلات، بس لسه فيه أخطاء لازم تتصلح قبل الاعتماد.", "error")
            return redirect(f"/invoices/{invoice['id']}{q}")
        flash(request, "اتحفظت الفاتورة" + (" واتعتمدت." if updated["approved"] else "."), "success")
        if action == "save_next":
            if form.get("next_id"):
                return redirect(f"/invoices/{form.get('next_id')}{q}")
            flash(request, "خلصت كل الفواتير اللي محتاجة مراجعة.", "success")
            return redirect("/review" if q else f"/clients/{client['id']}?period={updated['period']}")
        return redirect(f"/invoices/{invoice['id']}{q}")

    @login_required
    async def invoice_file(request: Request):
        invoice = get_invoice_or_404(request, request.path_params["invoice_id"])
        ref = invoice.get("stored_path")
        if not await run_in_threadpool(service.files.exists, ref):
            return PlainTextResponse("الملف مش موجود", status_code=404)
        headers = {"Cache-Control": "private, max-age=3600", "Content-Disposition": "inline"}
        if service.files.mode == "local":
            return FileResponse(Path(ref), headers=headers)
        content = await run_in_threadpool(service.files.read, ref)
        return Response(content, media_type=service.files.media_type(ref), headers=headers)

    @login_required
    async def invoice_page_image(request: Request):
        """First pages of a PDF rendered as PNG (PDF frames don't work well on phones)."""
        invoice = get_invoice_or_404(request, request.path_params["invoice_id"])
        page = request.path_params["page"]
        ref = invoice.get("stored_path")
        if service.files.suffix(ref) != ".pdf" or not 0 <= page < 5 or not await run_in_threadpool(service.files.exists, ref):
            return PlainTextResponse("not found", status_code=404)
        cached = service.files.page_cache_path(ref, page)
        if not cached.exists():
            def render_page():
                import pypdfium2 as pdfium
                content = service.files.read(ref)
                with PDFIUM_LOCK:
                    pdf = pdfium.PdfDocument(content)
                    try:
                        if page >= len(pdf):
                            return False
                        pdf[page].render(scale=2).to_pil().save(cached)
                        return True
                    finally:
                        pdf.close()
            if not await run_in_threadpool(render_page):
                return PlainTextResponse("not found", status_code=404)
        return FileResponse(cached, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    # -------------------------------------------------------------- whatsapp
    async def whatsapp_webhook(request: Request):
        if request.method == "GET":
            params = request.query_params
            if (params.get("hub.mode") == "subscribe" and settings.whatsapp_verify_token
                    and params.get("hub.verify_token") == settings.whatsapp_verify_token):
                return PlainTextResponse(params.get("hub.challenge", ""))
            return PlainTextResponse("forbidden", status_code=403)

        body = await request.body()
        if not whatsapp.verify_signature(settings.whatsapp_app_secret, body,
                                         request.headers.get("x-hub-signature-256")):
            return PlainTextResponse("bad signature", status_code=401)
        try:
            payload = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return PlainTextResponse("bad json", status_code=400)
        messages = whatsapp.extract_messages(payload)

        def work():
            for message in messages:
                try:
                    whatsapp.handle_message(message, db, service, wa)
                except Exception:
                    log.exception("WhatsApp message failed")

        if settings.serverless:
            # No life after the response on serverless: queue the files now (fast), reading comes later.
            await run_in_threadpool(work)
            return JSONResponse({"received": len(messages)})
        # Answer Meta right away; process after the response is sent.
        return JSONResponse({"received": len(messages)}, background=BackgroundTask(work))

    async def health(request: Request):
        def check_db():
            try:
                with db.connect() as conn:
                    conn.execute("SELECT 1 FROM accountants LIMIT 1").fetchone()
                return "ok"
            except Exception as exc:  # message only, never the connection string
                return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200] if str(exc) else ''}"
        def check_eta():
            """?eta=1: can this server reach the tax authority (identity + API hosts)?"""
            import requests as _rq
            from invoice_extractor.eta_client import ENVIRONMENTS
            hosts = ENVIRONMENTS.get(settings.eta_env)
            if not hosts:
                return settings.eta_env
            out = {}
            for name in ("identity", "api"):
                try:
                    r = _rq.get(hosts[name], timeout=10)
                    out[name] = f"reachable ({r.status_code})"
                except Exception as exc:
                    out[name] = f"unreachable: {type(exc).__name__}"
            return out
        extra = {"eta_env": settings.eta_env}
        if request.query_params.get("eta") == "1":
            extra["eta"] = await run_in_threadpool(check_eta)
        return JSONResponse({"status": "ok", "db": await run_in_threadpool(check_db), **extra,
                             "storage": settings.storage, "serverless": settings.serverless,
                             "ai": ai_vision.provider() if settings.use_ai else None,
                             "ocr": ocr.status() if settings.use_ocr else "off",
                             "whatsapp": settings.whatsapp_enabled})

    async def not_found(request: Request, exc):
        if request.url.path.startswith("/webhooks"):
            return PlainTextResponse("not found", status_code=404)
        return render(request, "404.html", status_code=404)

    async def lookup_error(request: Request, exc):
        return await not_found(request, exc)

    routes = [
        Route("/", dashboard),
        Route("/review", review_queue),
        Route("/invoices", invoices_page),
        Route("/invoices/export.csv", invoices_csv),
        Route("/api/search", api_search),
        Route("/analytics", analytics),
        Route("/signer", signer_page),
        Route("/settings", company_settings),
        Route("/clients/{client_id:int}/eta-test", client_eta_test, methods=["POST"]),
        Route("/login", login, methods=["GET", "POST"]),
        Route("/register", register, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST", "GET"]),
        Route("/clients", client_create, methods=["POST"]),
        Route("/clients/{client_id:int}", client_detail),
        Route("/clients/{client_id:int}/edit", client_update, methods=["POST"]),
        Route("/clients/{client_id:int}/upload", client_upload, methods=["POST"]),
        Route("/clients/{client_id:int}/progress.json", client_progress),
        Route("/internal/run-jobs", run_jobs, methods=["POST", "GET"]),
        Route("/jobs/kick", kick_jobs, methods=["POST"]),
        Route("/clients/{client_id:int}/export.xlsx", client_export),
        Route("/clients/{client_id:int}/approve", client_approve_bulk, methods=["POST"]),
        Route("/clients/{client_id:int}/report", client_report),
        Route("/clients/{client_id:int}/eta-sync", client_eta_sync, methods=["POST"]),
        Route("/clients/{client_id:int}/einvoice-settings", client_einvoice_settings, methods=["POST"]),
        Route("/clients/{client_id:int}/sales/new", client_new_sale, methods=["POST"]),
        Route("/invoices/{invoice_id:int}/issue", invoice_issue, methods=["GET", "POST"]),
        Route("/invoices/{invoice_id:int}", invoice_detail),
        Route("/invoices/{invoice_id:int}/edit", invoice_update, methods=["POST"]),
        Route("/invoices/{invoice_id:int}/file", invoice_file),
        Route("/invoices/{invoice_id:int}/page/{page:int}.png", invoice_page_image),
        Route("/webhooks/whatsapp", whatsapp_webhook, methods=["GET", "POST"]),
        Route("/health", health),
        Mount("/static", StaticFiles(directory=str(HERE / "static")), name="static"),
    ]
    middleware = [Middleware(SessionMiddleware, secret_key=settings.secret_key, session_cookie="iv_session",
                             max_age=14 * 24 * 3600, same_site="lax", https_only=settings.https_only_cookies)]
    app = Starlette(routes=routes, middleware=middleware,
                    exception_handlers={404: not_found, LookupError: lookup_error})
    app.state.db = db
    app.state.service = service
    try:
        service.encrypt_stored_secrets()
    except Exception:  # never keep the app from starting (e.g. database briefly unreachable)
        log.exception("Encrypting stored secrets failed")
    service.job_listeners.append(lambda job: whatsapp.reply_when_read(job, db, wa))
    service.start_workers(settings.upload_workers)
    app.state.settings = settings
    return app
