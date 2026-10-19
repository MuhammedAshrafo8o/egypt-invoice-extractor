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

from invoice_extractor.extractors import ai_vision, ocr

from . import whatsapp
from .config import Settings, load_settings
from .db import Database
from .exports import build_workbook
from .services import InvoiceService, UploadError, current_period, filing_deadline, valid_period

log = logging.getLogger(__name__)
HERE = Path(__file__).parent

STATUS_LABELS = {"ok": "سليمة", "review": "مراجعة", "error": "خطأ"}
SOURCE_LABELS = {
    "pdf_text": "نص PDF", "ai_vision": "ذكاء اصطناعي", "ocr": "OCR مجاني", "eta_qr": "QR المنظومة",
    "eta_api": "المنظومة", "unknown": "غير معروف",
}
CHANNEL_LABELS = {"upload": "رفع", "whatsapp": "واتساب", "eta_sync": "المنظومة"}
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
                       CHANNEL_LABELS=CHANNEL_LABELS)

    # ---------------------------------------------------------------- helpers
    def reader_status() -> dict:
        """Which engine reads photos / scanned PDFs right now (shown in the top bar)."""
        if settings.use_ai and ai_vision.is_available():
            return {"kind": "ai", "label": f"القراءة: {ai_vision.provider_label()}"}
        if settings.use_ocr and ocr.is_available():
            label = "القراءة: OCR مجاني" if "ara" in ocr.available_languages() else "OCR بدون عربي"
            return {"kind": "ocr", "label": label}
        return {"kind": "off", "label": "قراءة الصور مقفولة"}

    def render(request: Request, name: str, status_code: int = 200, **ctx):
        session = request.session
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(24)
        flashes = session.pop("flashes", [])
        user = db.get_accountant(session["uid"]) if session.get("uid") else None
        return templates.TemplateResponse(request, name, {
            "user": user, "csrf": session["csrf"], "flashes": flashes,
            "settings": settings, "reader": reader_status(),
            "pending_total": db.pending_count(user["id"]) if user else 0, **ctx,
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
            errors = []
            if not name:
                errors.append("اكتب اسمك أو اسم المكتب.")
            if "@" not in email:
                errors.append("الإيميل مش صحيح.")
            elif db.email_exists(email):
                errors.append("الإيميل ده متسجل قبل كده.")
            if len(password) < 8:
                errors.append("كلمة السر لازم تكون ٨ حروف على الأقل.")
            if errors:
                for e in errors:
                    flash(request, e, "error")
                return render(request, "register.html", name=name, email=email, status_code=400)
            uid = db.create_accountant(name, email, password)
            request.session.clear()
            request.session["uid"] = uid
            flash(request, "أهلًا بيك! ابدأ بإضافة أول عميل.", "success")
            return redirect("/")
        return render(request, "register.html")

    async def logout(request: Request):
        request.session.clear()
        return redirect("/login")

    # ------------------------------------------------------------- dashboard
    @login_required
    async def dashboard(request: Request):
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
        return render(request, "dashboard.html", clients=clients, period=period, nav="home",
                      periods=recent_periods(), totals=totals,
                      deadline=filing_deadline(settings.vat_deadline_day))

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
        period = valid_period(request.query_params.get("period"))
        status = request.query_params.get("status") or None
        direction = request.query_params.get("direction") or None
        invoices = db.list_invoices(client["id"], period, status, direction)
        return render(request, "client.html", client=client, invoices=invoices, period=period, nav="clients",
                      periods=recent_periods(db.periods(client["id"])), status=status, direction=direction,
                      summary=service.summary(client["id"], period))

    @login_required
    async def client_update(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        if form.get("action") == "delete":
            if form.get("confirm_name", "").strip() != client["name"]:
                flash(request, "اكتب اسم العميل بالظبط عشان تأكد المسح.", "error")
                return redirect(f"/clients/{client['id']}#settings")
            db.delete_client(client["id"])
            flash(request, f"اتمسح العميل {client['name']} وكل فواتيره.", "success")
            return redirect("/")
        phone = form.get("whatsapp_phone", "")
        if db.phone_taken(phone, except_client_id=client["id"]):
            flash(request, "رقم الواتساب ده متسجل لعميل تاني.", "error")
            return redirect(f"/clients/{client['id']}#settings")
        fields = {k: form.get(k, "") for k in ("name", "tax_id", "whatsapp_phone", "notes", "eta_client_id")}
        if form.get("eta_client_secret"):  # keep the stored secret unless a new one is typed
            fields["eta_client_secret"] = form.get("eta_client_secret")
        if form.get("clear_eta"):
            fields["eta_client_id"] = ""
            fields["eta_client_secret"] = ""
        if not fields["name"].strip():
            flash(request, "اسم العميل مطلوب.", "error")
        else:
            db.update_client(client["id"], **fields)
            flash(request, "اتحفظت بيانات العميل.", "success")
        return redirect(f"/clients/{client['id']}")

    @login_required
    async def client_upload(request: Request):
        client = get_client_or_404(request, request.path_params["client_id"])
        form = await form_with_csrf(request)
        files = [f for f in form.getlist("files") if getattr(f, "filename", "")]
        if not files:
            flash(request, "اختار ملف واحد على الأقل.", "error")
            return redirect(f"/clients/{client['id']}")
        viewed_period = valid_period(form.get("period"))
        new_ids, failed = [], []
        for upload in files:
            content = await upload.read()
            await upload.close()
            try:
                new_ids.append(await run_in_threadpool(service.add_file, client, upload.filename, content, "upload"))
            except UploadError as exc:
                failed.append(f"{upload.filename}: {exc}")
        for msg in failed:
            flash(request, msg, "error")
        if len(new_ids) == 1 and not failed:
            # One invoice: open it straight away so its fields are on screen.
            flash(request, "اتقرت الفاتورة — راجع البيانات، صلّح لو فيه غلط، واعتمدها.", "success")
            return redirect(f"/invoices/{new_ids[0]}")
        if new_ids:
            flash(request, f"اترفعت {len(new_ids)} فاتورة واتقرت. دوس على أي فاتورة عشان تشوف بياناتها.", "success")
            other = {}
            for inv_id in new_ids:
                p = db.get_invoice(inv_id)["period"]
                if p != viewed_period:
                    other[p] = other.get(p, 0) + 1
            for p, n in other.items():
                flash(request, f"{n} فاتورة اتسجلت على شهر {period_label(p)} حسب تاريخها — غيّر الشهر من فوق عشان تشوفها.",
                      "info")
        return redirect(f"/clients/{client['id']}?period={viewed_period}")

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
        if result.imported or result.skipped:
            flash(request, f"اتسحب {result.imported} فاتورة جديدة من المنظومة "
                           f"(واتخطّى {result.skipped} موجودين أو ملغيين).", "success")
        elif not result.errors:
            flash(request, "مفيش فواتير في المنظومة للشهر ده.", "info")
        for error in result.errors[:5]:
            flash(request, error, "error")
        return redirect(f"/clients/{client['id']}?period={period}")

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
        is_pdf = (invoice.get("stored_path") or "").lower().endswith(".pdf")
        page_count = 0
        if is_pdf and Path(invoice["stored_path"]).is_file():
            try:
                import pypdfium2 as pdfium
                pdf = pdfium.PdfDocument(invoice["stored_path"])
                page_count = min(len(pdf), 5)
                pdf.close()
            except Exception:
                page_count = 1
        return render(request, "invoice.html", invoice=invoice, client=client, next_id=ids[0] if ids else None,
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
            if invoice.get("stored_path"):
                stored = Path(invoice["stored_path"])
                for cached in stored.parent.glob(f"{stored.stem}.page*.png"):
                    cached.unlink(missing_ok=True)
                stored.unlink(missing_ok=True)
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
        path = Path(invoice.get("stored_path") or "")
        if not path.is_file() or settings.upload_dir.resolve() not in path.resolve().parents:
            return PlainTextResponse("الملف مش موجود", status_code=404)
        return FileResponse(path, headers={"Cache-Control": "private, max-age=3600",
                                           "Content-Disposition": "inline"})

    @login_required
    async def invoice_page_image(request: Request):
        """First pages of a PDF rendered as PNG (PDF frames don't work well on phones)."""
        invoice = get_invoice_or_404(request, request.path_params["invoice_id"])
        page = request.path_params["page"]
        path = Path(invoice.get("stored_path") or "")
        if not path.is_file() or path.suffix.lower() != ".pdf" or not 0 <= page < 5:
            return PlainTextResponse("not found", status_code=404)
        cached = path.with_name(f"{path.stem}.page{page}.png")
        if not cached.exists():
            def render_page():
                import pypdfium2 as pdfium
                pdf = pdfium.PdfDocument(str(path))
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

        # Answer Meta right away; process after the response is sent.
        return JSONResponse({"received": len(messages)}, background=BackgroundTask(work))

    async def health(request: Request):
        return JSONResponse({"status": "ok", "ai": ai_vision.provider() if settings.use_ai else None,
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
        Route("/login", login, methods=["GET", "POST"]),
        Route("/register", register, methods=["GET", "POST"]),
        Route("/logout", logout, methods=["POST", "GET"]),
        Route("/clients", client_create, methods=["POST"]),
        Route("/clients/{client_id:int}", client_detail),
        Route("/clients/{client_id:int}/edit", client_update, methods=["POST"]),
        Route("/clients/{client_id:int}/upload", client_upload, methods=["POST"]),
        Route("/clients/{client_id:int}/export.xlsx", client_export),
        Route("/clients/{client_id:int}/eta-sync", client_eta_sync, methods=["POST"]),
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
    app.state.settings = settings
    return app
