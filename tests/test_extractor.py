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


AI_ENV_KEYS = ("ANTHROPIC_API_KEY", "GEMINI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY",
               "AI_PROVIDER", "ANTHROPIC_BASE_URL", "GEMINI_BASE_URL")
FAKE_FIELDS = ('{"supplier_tax_id": "377-901-224", "invoice_date": "2026-10-10", "subtotal": 12500, '
               '"vat_amount": 1750, "total": 14250, "invoice_number": "GB-3301", "confidence": 0.95}')


class FakeAPIServer:
    """Tiny local HTTP server standing in for an AI provider."""

    def __init__(self, reply: dict):
        import json
        import threading
        from http.server import BaseHTTPRequestHandler, HTTPServer

        self.requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["content-length"])))
                outer.requests.append({"path": self.path, "body": body,
                                       "headers": {k.lower(): v for k, v in self.headers.items()}})
                data = json.dumps(reply).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def ai_env(**values):
    """Environment with only the given AI settings (others removed)."""
    import os

    env = {k: v for k, v in os.environ.items() if k not in AI_ENV_KEYS}
    env.update(NO_PROXY="127.0.0.1", no_proxy="127.0.0.1", **values)
    return mock.patch.dict(os.environ, env, clear=True)


class AIProviderTests(unittest.TestCase):
    """Exercise the real HTTP calls against local fakes of each provider's API."""

    def test_anthropic(self):
        fake = FakeAPIServer({"content": [{"type": "text", "text": "```json\n" + FAKE_FIELDS + "\n```"}]})
        with ai_env(ANTHROPIC_API_KEY="test-key", ANTHROPIC_BASE_URL=fake.url):
            self.assertEqual(ai_vision.provider(), "anthropic")
            data = ai_vision.extract_with_ai(SAMPLES / "05_photo.jpg")
            pdf_data = ai_vision.extract_with_ai(SAMPLES / "06_scanned.pdf")
        fake.close()
        first, last = fake.requests
        self.assertEqual(first["path"], "/v1/messages")
        self.assertEqual(first["headers"]["x-api-key"], "test-key")
        self.assertIn("anthropic-version", first["headers"])
        self.assertEqual(first["body"]["messages"][0]["content"][0]["type"], "image")
        self.assertEqual(last["body"]["messages"][0]["content"][0]["source"]["media_type"], "application/pdf")
        self.assertEqual((data["total"], pdf_data["invoice_number"]), (14250, "GB-3301"))

    def test_gemini(self):
        fake = FakeAPIServer({"candidates": [{"content": {"parts": [{"text": FAKE_FIELDS}]}}]})
        with ai_env(GEMINI_API_KEY="g-key", GEMINI_BASE_URL=fake.url, GEMINI_MODEL="gemini-test"):
            self.assertEqual(ai_vision.provider(), "gemini")
            data = ai_vision.extract_with_ai(SAMPLES / "06_scanned.pdf")
        fake.close()
        req = fake.requests[0]
        self.assertEqual(req["path"], "/models/gemini-test:generateContent")
        self.assertEqual(req["headers"]["x-goog-api-key"], "g-key")
        parts = req["body"]["contents"][0]["parts"]
        self.assertEqual(parts[0]["inline_data"]["mime_type"], "application/pdf")
        self.assertEqual(req["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertEqual(data["vat_amount"], 1750)

    def test_openai_compatible_renders_pdf_to_image(self):
        fake = FakeAPIServer({"choices": [{"message": {"content": FAKE_FIELDS}}]})
        with ai_env(OPENAI_BASE_URL=fake.url + "/v1", OPENAI_MODEL="qwen2.5vl"):
            self.assertEqual(ai_vision.provider(), "openai")
            data = ai_vision.extract_with_ai(SAMPLES / "06_scanned.pdf")
        fake.close()
        req = fake.requests[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertNotIn("authorization", req["headers"])  # Ollama needs no key
        content = req["body"]["messages"][0]["content"]
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertEqual(data["total"], 14250)

    def test_provider_selection(self):
        with ai_env():
            self.assertIsNone(ai_vision.provider())
        with ai_env(ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g"):
            self.assertEqual(ai_vision.provider(), "anthropic")
        with ai_env(ANTHROPIC_API_KEY="a", GEMINI_API_KEY="g", AI_PROVIDER="gemini"):
            self.assertEqual(ai_vision.provider(), "gemini")
        with ai_env(AI_PROVIDER="gemini"):  # chosen but not configured
            self.assertIsNone(ai_vision.provider())


from invoice_extractor.extractors import ocr  # noqa: E402


class OCRTests(unittest.TestCase):
    @unittest.skipUnless(ocr.status() == "ok", "Tesseract with Arabic is not installed")
    def test_photos_read_offline(self):
        with ai_env():
            invoices = {i.source_file: i for i in process_many(
                [SAMPLES / "05_photo.jpg", SAMPLES / "09_arabic_photo.jpg"])}
        en, ar = invoices["05_photo.jpg"], invoices["09_arabic_photo.jpg"]
        self.assertEqual((en.supplier_tax_id, en.subtotal, en.vat_amount, en.total),
                         ("377901224", 12500.0, 1750.0, 14250.0))
        self.assertEqual((ar.supplier_tax_id, ar.invoice_number, ar.subtotal, ar.vat_amount, ar.total),
                         ("615904382", "7781", 2300.0, 322.0, 2622.0))
        for inv in (en, ar):
            self.assertEqual(inv.source_type, "ocr")
            self.assertEqual(inv.status, "review")  # OCR results always get a human look
            self.assertIn("ocr_used", [i.code for i in inv.issues])

    @unittest.skipUnless(ocr.status() == "ok", "Tesseract with Arabic is not installed")
    def test_falls_back_to_ocr_when_ai_fails(self):
        with ai_env(GEMINI_API_KEY="x"), mock.patch.object(
                ai_vision, "extract_with_ai", side_effect=ai_vision.AIExtractionError("Rate limit reached")):
            [inv] = process_many([SAMPLES / "06_scanned.pdf"])
        codes = [i.code for i in inv.issues]
        self.assertIn("ai_failed", codes)
        self.assertEqual((inv.source_type, inv.total), ("ocr", 14250.0))

    def test_nothing_available_message(self):
        with ai_env(), mock.patch.object(ocr, "is_available", return_value=False):
            [inv] = process_many([SAMPLES / "05_photo.jpg"])
        self.assertIn("ai_unavailable", [i.code for i in inv.issues])
        self.assertEqual(inv.status, "error")


if __name__ == "__main__":
    unittest.main()
