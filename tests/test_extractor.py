"""Run with:  python -m unittest discover tests   (or: pytest)"""
from __future__ import annotations

import subprocess
import sys
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from invoice_extractor import Invoice, ValidationConfig, process_many, validate_invoice, vat_summary  # noqa: E402
from invoice_extractor.extractors import ai_vision  # noqa: E402
from invoice_extractor.extractors.qr import parse_eta_qr  # noqa: E402
from invoice_extractor.extractors.text_parser import parse_text  # noqa: E402
from invoice_extractor.normalize import normalize_text, parse_amount, parse_date  # noqa: E402

SAMPLES = ROOT / "samples" / "invoices"


class NormalizeTests(unittest.TestCase):
    def test_arabic_digits_and_separators(self):
        self.assertEqual(parse_amount("١٬٢٣٤٫٥٠"), 1234.5)
        self.assertEqual(parse_amount("EGP 11,400.00"), 11400.0)
        self.assertEqual(parse_amount("1.234,50"), 1234.5)
        self.assertIsNone(parse_amount("لا يوجد"))

    def test_dates_are_day_first(self):
        self.assertEqual(parse_date("٠٥/٠٣/٢٠٢٦"), "2026-03-05")
        self.assertEqual(parse_date("Date: 2026-03-05"), "2026-03-05")
        self.assertEqual(parse_date("05-03-26"), "2026-03-05")

    def test_letter_normalisation(self):
        self.assertEqual(normalize_text("الإجمالى فاتورة"), "الاجمالي فاتوره")


class ArabicParserTests(unittest.TestCase):
    TEXT = """شركة النيل للتوريدات
    رقم التسجيل الضريبي: ٥١٢-٣٣٤-٩٠١
    رقم الفاتورة: ١٤٧
    تاريخ الفاتورة: ١٢/١٠/٢٠٢٦
    اسم العميل: شركة الدلتا للتجارة
    الرقم الضريبي للعميل: ٣٠٠١١٢٤٥٥
    الإجمالي قبل الضريبة: ١٠٬٠٠٠٫٠٠
    ضريبة القيمة المضافة ١٤٪: ١٬٤٠٠٫٠٠
    الإجمالي شامل الضريبة: ١١٬٤٠٠٫٠٠ جنيه"""

    def test_fields(self):
        r = parse_text(self.TEXT.replace("٪", "%"))
        self.assertEqual(r["supplier_tax_id"], "512334901")
        self.assertEqual(r["buyer_tax_id"], "300112455")
        self.assertEqual(r["invoice_number"], "147")
        self.assertEqual(r["invoice_date"], "2026-10-12")
        self.assertEqual(r["subtotal"], 10000.0)
        self.assertEqual(r["vat_amount"], 1400.0)
        self.assertEqual(r["vat_rate"], 0.14)
        self.assertEqual(r["total"], 11400.0)
        self.assertEqual(r["supplier_name"], "شركة النيل للتوريدات")
        self.assertEqual(r["buyer_name"], "شركة الدلتا للتجارة")
        self.assertEqual(r["currency"], "EGP")

    def test_total_incl_vat_is_not_read_as_vat(self):
        r = parse_text("Subtotal: 100.00\nVAT 14%: 14.00\nTotal incl. VAT: 114.00")
        self.assertEqual((r["subtotal"], r["vat_amount"], r["total"]), (100.0, 14.0, 114.0))


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = ValidationConfig(today=date(2026, 10, 19))

    def _inv(self, **kw):
        base = dict(source_file="x", supplier_tax_id="512334901", invoice_number="1",
                    invoice_date="2026-10-01", subtotal=1000.0, vat_amount=140.0, total=1140.0)
        base.update(kw)
        return validate_invoice(Invoice(**base), self.cfg)

    def codes(self, inv):
        return {i.code for i in inv.issues}

    def test_clean_invoice_ok(self):
        self.assertEqual(self._inv().status, "ok")

    def test_bad_tax_id(self):
        inv = self._inv(supplier_tax_id="12345")
        self.assertIn("supplier_tax_id_format", self.codes(inv))
        self.assertEqual(inv.status, "error")

    def test_total_mismatch(self):
        self.assertIn("total_mismatch", self.codes(self._inv(total=1200.0)))

    def test_unusual_vat_rate(self):
        inv = self._inv(vat_amount=100.0, total=1100.0)
        self.assertIn("vat_rate_unusual", self.codes(inv))
        self.assertEqual(inv.status, "review")

    def test_future_date(self):
        self.assertIn("date_future", self.codes(self._inv(invoice_date="2026-12-10")))

    def test_old_date(self):
        self.assertIn("date_old", self.codes(self._inv(invoice_date="2025-01-10")))

    def test_subtotal_derived(self):
        inv = self._inv(subtotal=None)
        self.assertEqual(inv.subtotal, 1000.0)
        self.assertIn("subtotal_derived", self.codes(inv))

    def test_missing_required(self):
        inv = self._inv(total=None, supplier_tax_id=None)
        self.assertTrue({"total_missing", "supplier_tax_id_missing"} <= self.codes(inv))


class QRTests(unittest.TestCase):
    def test_eta_url(self):
        r = parse_eta_qr("https://invoicing.eta.gov.eg/print/documents/ABC123XYZ/share/9")
        self.assertEqual(r["eta_uuid"], "ABC123XYZ")
        self.assertIsNone(parse_eta_qr("https://example.com/qr"))


class AIResponseTests(unittest.TestCase):
    def test_json_with_fences(self):
        self.assertEqual(ai_vision._parse_json('```json\n{"total": 5}\n```'), {"total": 5})

    def test_ai_result_is_merged_for_photos(self):
        fake = {"supplier_tax_id": "377-901-224", "invoice_number": "GB-3301",
                "invoice_date": (date.today() - timedelta(days=3)).isoformat(),
                "subtotal": "12,500.00", "vat_rate": 14, "vat_amount": 1750, "total": 14250,
                "supplier_name": "Giza Building Materials", "currency": "EGP", "confidence": 0.93}
        with mock.patch.object(ai_vision, "is_available", return_value=True), \
             mock.patch.object(ai_vision, "extract_with_ai", return_value=fake):
            [inv] = process_many([SAMPLES / "05_photo.jpg"])
        self.assertEqual(inv.source_type, "ai_vision")
        self.assertEqual(inv.supplier_tax_id, "377901224")
        self.assertEqual(inv.subtotal, 12500.0)
        self.assertEqual(inv.vat_rate, 0.14)
        self.assertEqual(inv.status, "ok")


class PipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        subprocess.run([sys.executable, str(ROOT / "samples" / "make_samples.py")], check=True,
                       capture_output=True)
        files = sorted(SAMPLES.glob("0[1-4]_*.pdf"))
        cls.invoices = {i.source_file: i for i in process_many(files, use_ai=False)}

    def test_statuses(self):
        s = {k: v.status for k, v in self.invoices.items()}
        self.assertEqual(s["01_digital_ok.pdf"], "ok")
        self.assertEqual(s["02_total_mismatch.pdf"], "error")
        self.assertEqual(s["03_duplicate_of_01.pdf"], "error")
        self.assertEqual(s["04_eta_einvoice.pdf"], "review")

    def test_eta_qr_detected(self):
        inv = self.invoices["04_eta_einvoice.pdf"]
        self.assertEqual(inv.source_type, "eta_qr")
        self.assertTrue(inv.eta_uuid.startswith("SAMPLE"))

    def test_vat_summary_uses_only_valid_invoices(self):
        summary = vat_summary(list(self.invoices.values()))
        self.assertEqual(summary["vat_sum"], 1400.0 + 1015.0)
        self.assertEqual(summary["invoices_need_fixing"], 2)


if __name__ == "__main__":
    unittest.main()


class AIHttpTests(unittest.TestCase):
    """Exercise the real HTTP call against a local fake of the Messages API."""

    def test_request_format_and_parsing(self):
        import json
        import os
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        seen = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                seen.update(headers={k.lower(): v for k, v in self.headers.items()}, body=body, path=self.path)
                reply = {"content": [{"type": "text", "text": '```json\n{"supplier_tax_id": "377-901-224", '
                                      '"invoice_date": "2026-10-10", "subtotal": 12500, "vat_amount": 1750, '
                                      '"total": 14250, "invoice_number": "GB-3301", "confidence": 0.95}\n```'}]}
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {"ANTHROPIC_API_KEY": "test-key", "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
               "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
        with mock.patch.dict(os.environ, env):
            data = ai_vision.extract_with_ai(SAMPLES / "05_photo.jpg")
            pdf_data = ai_vision.extract_with_ai(SAMPLES / "06_scanned.pdf")
        server.shutdown()

        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["headers"]["x-api-key"], "test-key")
        self.assertIn("anthropic-version", seen["headers"])
        content = seen["body"]["messages"][0]["content"]
        self.assertEqual(content[0]["type"], "document")  # last call was the PDF
        self.assertEqual(content[0]["source"]["media_type"], "application/pdf")
        self.assertEqual(data["total"], 14250)
        self.assertEqual(pdf_data["invoice_number"], "GB-3301")
