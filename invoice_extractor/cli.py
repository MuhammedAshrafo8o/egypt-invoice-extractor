"""Command line: python -m invoice_extractor <files or folders> [--out results/]"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
from pathlib import Path

from .pipeline import SUPPORTED_EXTS, process_many, vat_summary
from .validation import ValidationConfig

CSV_COLUMNS = (
    "status", "source_file", "source_type", "supplier_name", "supplier_tax_id",
    "invoice_number", "invoice_date", "currency", "subtotal", "vat_rate",
    "vat_amount", "total", "buyer_name", "buyer_tax_id", "eta_uuid", "issues",
)


def collect_files(inputs: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in inputs:
        p = Path(item)
        if p.is_dir():
            files.extend(sorted(f for f in p.rglob("*") if f.suffix.lower() in SUPPORTED_EXTS))
        elif p.exists():
            files.append(p)
        else:
            print(f"! not found: {item}", file=sys.stderr)
    return files


def write_csv(invoices, path: Path) -> None:
    # utf-8-sig so Excel opens Arabic text correctly
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for inv in invoices:
            row = {k: v for k, v in inv.to_dict().items() if k in CSV_COLUMNS}
            row["issues"] = " | ".join(f"[{i.level}] {i.message}" for i in inv.issues)
            writer.writerow(row)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="invoice_extractor",
        description="Extract and validate data from Egyptian invoices (PDF / images).",
    )
    parser.add_argument("inputs", nargs="+", help="Invoice files or folders")
    parser.add_argument("--out", default="results", help="Output folder (default: results)")
    parser.add_argument("--no-ai", action="store_true", help="Don't call the AI model for scanned files/photos")
    parser.add_argument("--max-age-days", type=int, default=180, help="Flag invoices older than this")
    parser.add_argument("--raw-text", action="store_true", help="Include extracted PDF text in the JSON")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING)
    files = collect_files(args.inputs)
    if not files:
        print("No supported files found (pdf, jpg, png, webp, tif).", file=sys.stderr)
        return 1

    cfg = ValidationConfig(max_age_days=args.max_age_days)
    invoices = process_many(files, use_ai=not args.no_ai, cfg=cfg)
    summary = vat_summary(invoices)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "invoices.json").write_text(
        json.dumps({"summary": summary, "invoices": [i.to_dict(args.raw_text) for i in invoices]},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    write_csv(invoices, out / "invoices.csv")

    icons = {"ok": "✅", "review": "🟡", "error": "🔴"}
    for inv in invoices:
        total = f"{inv.total:,.2f}" if inv.total is not None else "—"
        print(f"{icons.get(inv.status, '?')} {inv.source_file:<35} total={total:<12} {inv.source_type}")
        for issue in inv.issues:
            print(f"     [{issue.level}] {issue.message}")
    print()
    print(f"Files: {summary['invoices_total']}  |  OK/review: {summary['invoices_usable']}  |  "
          f"Need fixing: {summary['invoices_need_fixing']}")
    print(f"Subtotal: {summary['subtotal_sum']:,.2f}  |  VAT: {summary['vat_sum']:,.2f}  |  "
          f"Total: {summary['total_sum']:,.2f}  (EGP, usable invoices)")
    print(f"Saved: {out / 'invoices.json'} and {out / 'invoices.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
