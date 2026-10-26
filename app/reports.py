"""Company reports: what the company sold and bought, the VAT it owes, who it sells to and buys from,
and how much of it is on the e-invoice system. Built from the same invoices as the VAT summary
(errors not counted unless approved, EGP only), so the numbers always agree with the home page."""
from __future__ import annotations

from collections import defaultdict
from typing import Optional


def _prev(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _pct(now: float, before: float) -> Optional[int]:
    if not before:
        return None
    return round(100 * (now - before) / abs(before))


def _counted(invoices: list[dict]) -> list[dict]:
    return [i for i in invoices if (i["status"] != "error" or i["approved"]) and (i["currency"] or "EGP") == "EGP"]


def month_figures(invoices: list[dict]) -> dict:
    rows = _counted(invoices)
    sales = [i for i in rows if i["direction"] == "sale"]
    buys = [i for i in rows if i["direction"] == "purchase"]

    def s(items, key):
        return round(sum(i[key] or 0 for i in items), 2)

    return {
        "sales": s(sales, "subtotal"), "sales_vat": s(sales, "vat_amount"), "sales_total": s(sales, "total"),
        "sales_count": len(sales),
        "purchases": s(buys, "subtotal"), "purchases_vat": s(buys, "vat_amount"), "purchases_total": s(buys, "total"),
        "purchases_count": len(buys),
        "pending": sum(1 for i in invoices if i["status"] in ("review", "error") and not i["approved"]),
    }


def _parties(rows: list[dict], name_key: str, tax_key: str, limit: int = 8) -> list[dict]:
    groups: dict = defaultdict(lambda: {"name": "", "tax_id": "", "amount": 0.0, "vat": 0.0, "count": 0})
    for i in rows:
        key = (i[tax_key] or "").strip() or (i[name_key] or "").strip() or "—"
        g = groups[key]
        g["name"] = g["name"] or (i[name_key] or i["original_filename"] or "غير معروف")
        g["tax_id"] = g["tax_id"] or (i[tax_key] or "")
        g["amount"] += i["subtotal"] or 0
        g["vat"] += i["vat_amount"] or 0
        g["count"] += 1
    total = sum(g["amount"] for g in groups.values()) or 1
    out = sorted(groups.values(), key=lambda g: -g["amount"])
    for g in out:
        g["amount"], g["vat"] = round(g["amount"], 2), round(g["vat"], 2)
        g["share"] = round(100 * g["amount"] / total)
    return out[:limit]


def company_report(db, client, period: str, months: int = 12) -> dict:
    periods = [period]
    while len(periods) < months:
        periods.insert(0, _prev(periods[0]))
    invoices = {p: db.list_invoices(client["id"], p) for p in periods}
    by_month = {p: month_figures(invoices[p]) for p in periods}
    cur, prev_p = by_month[period], _prev(period)
    before = by_month.get(prev_p) or month_figures(db.list_invoices(client["id"], prev_p))

    # VAT owed month by month this year: a credit (more input than output VAT) carries to the next month.
    year = [p for p in periods if p[:4] == period[:4]]
    carry, vat_rows = 0.0, []
    for p in year:
        f = by_month[p]
        due = f["sales_vat"] - f["purchases_vat"] - carry
        payable, carry_after = (round(due, 2), 0.0) if due >= 0 else (0.0, round(-due, 2))
        vat_rows.append({"period": p, "output": f["sales_vat"], "input": f["purchases_vat"],
                         "carried_in": round(carry, 2), "payable": payable, "carry_out": carry_after})
        carry = carry_after
    this_vat = vat_rows[-1]

    # don't show the empty months before the company's first invoice
    first = next((i for i, p in enumerate(periods)
                  if by_month[p]["sales_count"] or by_month[p]["purchases_count"]), len(periods) - 1)
    shown = periods[min(first, len(periods) - 3):]
    vat_rows = [r for r in vat_rows if r["period"] in shown]

    ytd = {k: round(sum(by_month[p][k] for p in year), 2)
           for k in ("sales", "sales_vat", "purchases", "purchases_vat", "sales_count", "purchases_count")}
    ytd["vat_paid"] = round(sum(r["payable"] for r in vat_rows), 2)

    rows = _counted(invoices[period])
    sales_rows = [i for i in rows if i["direction"] == "sale"]
    buy_rows = [i for i in rows if i["direction"] == "purchase"]
    customers = _parties(sales_rows, "buyer_name", "buyer_tax_id")
    suppliers = _parties(buy_rows, "supplier_name", "supplier_tax_id")

    # e-invoices: sales that went to the ETA, purchases the ETA confirms
    issued = [i for i in sales_rows if i.get("eta_uuid")]
    rejected = [i for i in sales_rows if i.get("eta_state") in ("rejected", "invalid")]
    not_issued = [i for i in sales_rows if not i.get("eta_uuid") and i not in rejected]
    matched = [i for i in buy_rows if i.get("eta_check") == "matched" or i["channel"] == "eta_sync"]
    missing = [i for i in buy_rows if i.get("eta_check") == "missing"]
    einvoice = {
        "sales_issued": len(issued), "sales_rejected": len(rejected), "sales_not_issued": len(not_issued),
        "sales_not_issued_vat": round(sum(i["vat_amount"] or 0 for i in not_issued), 2),
        "purchases_matched": len(matched), "purchases_missing": len(missing),
        "purchases_missing_vat": round(sum(i["vat_amount"] or 0 for i in missing), 2),
        "purchases_unchecked": len(buy_rows) - len(matched) - len(missing),
    }

    sales_change = _pct(cur["sales"], before["sales"])
    buys_change = _pct(cur["purchases"], before["purchases"])
    insights = []
    if cur["sales"] or before["sales"]:
        if sales_change is not None and sales_change != 0:
            insights.append(f"مبيعاتك {'زادت' if sales_change > 0 else 'قلّت'} {abs(sales_change)}٪ عن الشهر اللي فات.")
    if this_vat["payable"]:
        insights.append(f"عليك ضريبة قيمة مضافة {this_vat['payable']:,.2f} جنيه عن الشهر ده.")
    elif this_vat["carry_out"]:
        insights.append(f"ليك رصيد دائن {this_vat['carry_out']:,.2f} جنيه بيترحّل للشهر الجاي.")
    if customers and customers[0]["share"] >= 40 and len(customers) > 1:
        insights.append(f"{customers[0]['name']} لوحده {customers[0]['share']}٪ من مبيعاتك. اعتمادك على عميل واحد عالي.")
    if einvoice["purchases_missing"]:
        insights.append(f"{einvoice['purchases_missing']} فاتورة مشتريات مش على المنظومة، وضريبتها "
                        f"{einvoice['purchases_missing_vat']:,.2f} جنيه ممكن متتخصمش. اطلب من الموردين يصدروها.")
    if einvoice["sales_not_issued"]:
        insights.append(f"{einvoice['sales_not_issued']} فاتورة مبيعات لسه ماطلعتش فاتورة إلكترونية.")
    if cur["pending"]:
        insights.append(f"{cur['pending']} فاتورة لسه محتاجة مراجعة، والأرقام دي مبدئية لحد ما تتراجع.")

    return {
        "period": period, "prev": prev_p, "periods": shown, "by_month": by_month, "cur": cur, "before": before,
        "sales_change": sales_change, "purchases_change": buys_change,
        "gap": round(cur["sales"] - cur["purchases"], 2),
        "vat": this_vat, "vat_rows": vat_rows, "ytd": ytd,
        "customers": customers, "suppliers": suppliers, "einvoice": einvoice, "insights": insights,
    }
