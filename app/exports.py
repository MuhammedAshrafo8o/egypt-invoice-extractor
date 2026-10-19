"""Excel export of a client's month: purchases, sales and the VAT summary (Arabic, RTL)."""
from __future__ import annotations

import io

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

HEADER_FILL = PatternFill("solid", fgColor="1F4E46")
HEADER_FONT = Font(bold=True, color="FFFFFF", name="Arial")
BODY_FONT = Font(name="Arial")
TOTAL_FONT = Font(bold=True, name="Arial")
TOTAL_FILL = PatternFill("solid", fgColor="E8F0EE")
WARN_FILL = PatternFill("solid", fgColor="FFF4D6")
THIN = Side(style="thin", color="D0D7D5")
BORDER = Border(top=THIN, bottom=THIN, left=THIN, right=THIN)
MONEY = '#,##0.00;[Red]-#,##0.00'

COLUMNS = [
    ("م", 6, None),
    ("تاريخ الفاتورة", 14, None),
    ("رقم الفاتورة", 16, None),
    ("اسم الطرف الآخر", 32, None),
    ("الرقم الضريبي", 14, None),
    ("المبلغ قبل الضريبة", 18, MONEY),
    ("نسبة الضريبة", 12, "0%"),
    ("قيمة الضريبة", 16, MONEY),
    ("الإجمالي", 18, MONEY),
    ("الحالة", 14, None),
    ("المصدر", 14, None),
    ("ملاحظات", 50, None),
]
STATUS_AR = {"ok": "سليمة", "review": "مراجعة", "error": "خطأ"}
SOURCE_AR = {"upload": "رفع ملف", "whatsapp": "واتساب", "eta_sync": "المنظومة"}


def _status(inv: dict) -> str:
    if inv["approved"]:
        return "معتمدة"
    return STATUS_AR.get(inv["status"], inv["status"])


def _sheet(wb: Workbook, title: str, invoices: list[dict], counterparty: str) -> tuple[str, int]:
    ws = wb.create_sheet(title)
    ws.sheet_view.rightToLeft = True
    for col, (name, width, _) in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=1, column=col, value=name)
        cell.fill, cell.font, cell.border = HEADER_FILL, HEADER_FONT, BORDER
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[get_column_letter(col)].width = width
    ws.row_dimensions[1].height = 30
    ws.freeze_panes = "A2"

    row = 1
    for n, inv in enumerate(invoices, start=1):
        row += 1
        name_key, tax_key = (("supplier_name", "supplier_tax_id") if counterparty == "supplier"
                             else ("buyer_name", "buyer_tax_id"))
        notes = " | ".join(i.get("message_ar") or i.get("message", "") for i in inv["issues"])
        values = [n, inv["invoice_date"], inv["invoice_number"], inv[name_key], inv[tax_key],
                  inv["subtotal"], inv["vat_rate"], inv["vat_amount"], inv["total"], _status(inv),
                  SOURCE_AR.get(inv["channel"], inv["channel"]), notes]
        counted = inv["status"] != "error" or inv["approved"]
        for col, value in enumerate(values, start=1):
            cell = ws.cell(row=row, column=col, value=value)
            cell.font, cell.border = BODY_FONT, BORDER
            fmt = COLUMNS[col - 1][2]
            if fmt:
                cell.number_format = fmt
            if not counted:
                cell.fill = WARN_FILL

    total_row = row + 1
    ws.cell(row=total_row, column=4, value="الإجمالي (الفواتير المحتسبة)")
    if row >= 2:
        # Errors that are not approved are excluded (column J = status).
        for col in (6, 8, 9):
            letter = get_column_letter(col)
            ws.cell(row=total_row, column=col,
                    value=f'=SUMIFS({letter}2:{letter}{row},J2:J{row},"<>خطأ")').number_format = MONEY
    else:
        for col in (6, 8, 9):
            ws.cell(row=total_row, column=col, value=0).number_format = MONEY
    for col in range(1, len(COLUMNS) + 1):
        cell = ws.cell(row=total_row, column=col)
        cell.font, cell.fill, cell.border = TOTAL_FONT, TOTAL_FILL, BORDER
    return title, total_row


def build_workbook(client: dict, period: str, invoices: list[dict], summary: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "ملخص الإقرار"
    ws.sheet_view.rightToLeft = True

    purchases = [i for i in invoices if i["direction"] == "purchase"]
    sales = [i for i in invoices if i["direction"] == "sale"]
    p_title, p_total = _sheet(wb, "المشتريات", purchases, "supplier")
    s_title, s_total = _sheet(wb, "المبيعات", sales, "buyer")

    ws.column_dimensions["A"].width = 38
    ws.column_dimensions["B"].width = 22
    rows = [
        ("العميل", client["name"]),
        ("الرقم الضريبي", client["tax_id"] or "—"),
        ("الفترة", period),
        None,
        ("المبيعات قبل الضريبة", f"='{s_title}'!F{s_total}"),
        ("ضريبة المخرجات (على المبيعات)", f"='{s_title}'!H{s_total}"),
        ("المشتريات قبل الضريبة", f"='{p_title}'!F{p_total}"),
        ("ضريبة المدخلات (على المشتريات)", f"='{p_title}'!H{p_total}"),
        ("صافي الضريبة (موجب = مستحق للسداد)", "=B7-B9"),
        None,
        ("عدد الفواتير", summary["invoice_count"]),
        ("فواتير فيها أخطاء (مش محتسبة)", summary["error_count"]),
        ("فواتير محتاجة مراجعة", summary["review_count"]),
    ]
    ws.cell(row=1, column=1, value="ملخص إقرار ضريبة القيمة المضافة").font = Font(bold=True, size=14, name="Arial")
    for offset, item in enumerate(rows, start=2):
        if item is None:
            continue
        label, value = item
        a = ws.cell(row=offset, column=1, value=label)
        b = ws.cell(row=offset, column=2, value=value)
        a.font, b.font = TOTAL_FONT, BODY_FONT
        a.border = b.border = BORDER
        if isinstance(value, str) and value.startswith("="):
            b.number_format = MONEY
    # Rows start at 2: B7 = output VAT, B9 = input VAT, B10 = net.
    ws["A10"].fill = ws["B10"].fill = TOTAL_FILL
    ws["A15"] = "ملحوظة: الأرقام دي مساعدة لتجهيز الإقرار، ولازم المحاسب يراجعها قبل التقديم."
    ws["A15"].font = Font(italic=True, color="666666", name="Arial")

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()
