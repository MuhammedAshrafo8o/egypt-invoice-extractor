"""E-invoice issuing (sales) and matching purchases with the ETA.

The ETA itself is replaced by the local demo simulator (ETA_ENV=demo), which checks the
totals and that the CMS signature covers the serialized document. The CMS structure is
also verified independently with `openssl cms -verify` when openssl is installed.
"""
from __future__ import annotations

import base64
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from test_app import PERIOD, AppTestCase

from app.db import Database
from invoice_extractor.eta_demo import DemoETAClient
from invoice_extractor.eta_issue import (Draft, Line, Party, build_document, check_draft, document_hash_input,
                                         issued_at, serialize)
from invoice_extractor.eta_signer import PfxSigner, make_test_certificate, sign_document_bytes

ISSUER = Party(id="300112455", name="شركة الدلتا", governate="الإسكندرية", region_city="سموحة",
               street="فوزي معاذ", building_number="12")
PROFILE = {"activity_code": "4690", "item_code": "EG-300112455-1001", "item_type": "EGS"}
BUYER = Party(type="B", id="488201736", name="شركة الأهرام", governate="القاهرة", region_city="مدينة نصر",
              street="مصطفى النحاس", building_number="7")


def sample_draft(**kw) -> Draft:
    return Draft(receiver=kw.get("receiver", BUYER),
                 lines=kw.get("lines", [Line("مواسير", 20, 350, discount=200), Line("تركيب", 1, 1500, unit_type="HUR"),
                                        Line("كتب", 2, 100, vat_kind="exempt")]),
                 internal_id="INV-0001", issue_date=kw.get("issue_date", date.today().isoformat()))


class DocumentTests(unittest.TestCase):
    def test_serialization_rules(self):
        doc = {"internalID": "1", "amount": 0.5, "count": 3,
               "items": [{"code": "T1"}, {"code": "T2"}], "issuer": {"id": "9"}}
        self.assertEqual(serialize(doc), '"INTERNALID""1""AMOUNT""0.5""COUNT""3"'
                                         '"ITEMS""ITEMS""CODE""T1""ITEMS""CODE""T2""ISSUER""ID""9"')

    def test_totals_and_document(self):
        draft = sample_draft()
        totals = draft.totals()
        # 7000 - 200 = 6800 (+952), 1500 (+210), 200 exempt
        self.assertEqual(totals, {"sales": 8700.0, "discount": 200.0, "net": 8500.0, "vat": 1162.0, "total": 9662.0})
        doc = build_document(draft, ISSUER, PROFILE)
        self.assertEqual((doc["documentType"], doc["documentTypeVersion"], doc["netAmount"], doc["totalAmount"]),
                         ("I", "1.0", 8500, 9662))
        self.assertEqual(doc["taxTotals"], [{"taxType": "T1", "amount": 1162}])
        first, _, exempt = doc["invoiceLines"]
        self.assertEqual((first["salesTotal"], first["netTotal"], first["total"], first["discount"]["amount"]),
                         (7000, 6800, 7752, 200))
        self.assertEqual(first["taxableItems"][0], {"taxType": "T1", "amount": 952, "subType": "V009", "rate": 14})
        self.assertEqual(exempt["taxableItems"][0]["subType"], "V003")
        self.assertEqual(first["itemCode"], "EG-300112455-1001")
        self.assertEqual(doc["issuer"]["address"]["branchID"], "0")
        # Every line: total = net + taxes, and the document adds up (what the ETA checks first).
        for line in doc["invoiceLines"]:
            self.assertAlmostEqual(line["total"], line["netTotal"] + line["taxableItems"][0]["amount"])
        self.assertEqual(check_draft(draft, ISSUER, PROFILE), [])

    def test_draft_problems_in_arabic(self):
        draft = sample_draft(receiver=Party(type="B", id="123", name=""),
                             issue_date=date(2099, 1, 1).isoformat(), lines=[Line("", 0, 10)])
        problems = " ".join(check_draft(draft, ISSUER, {}))
        for text in ("كود النشاط", "الرقم الضريبي للمشتري", "اسم المشتري", "عنوان المشتري", "في المستقبل",
                     "الوصف ناقص", "الكمية", "كود الصنف"):
            self.assertIn(text, problems)
        person = sample_draft(receiver=Party(type="P"), lines=[Line("x", 1, 100)])
        self.assertEqual(check_draft(person, ISSUER, PROFILE), [])  # small sale to a person: no ID needed
        big = sample_draft(receiver=Party(type="P"), lines=[Line("x", 1, 60000)])
        self.assertIn("الرقم القومي", " ".join(check_draft(big, ISSUER, PROFILE)))

    def test_issue_time_is_utc_and_not_future(self):
        now = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(issued_at("2026-10-19", now), "2026-10-19T11:55:00Z")
        self.assertEqual(issued_at("2026-10-01", now), "2026-10-01T08:00:00Z")


class SignatureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.pfx = make_test_certificate(cls.tmp / "t.pfx", "300112455", "شركة الدلتا", "secret")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def signed_document(self):
        doc = build_document(sample_draft(), ISSUER, PROFILE)
        content = document_hash_input(doc)
        doc["signatures"] = [{"signatureType": "I", "value": sign_document_bytes(content, PfxSigner(self.pfx, "secret"))}]
        return doc, content

    def test_wrong_password(self):
        from invoice_extractor.eta_signer import SigningError
        with self.assertRaises(SigningError):
            PfxSigner(self.pfx, "wrong")

    @unittest.skipUnless(shutil.which("openssl"), "openssl not installed")
    def test_cms_verifies_with_openssl(self):
        doc, content = self.signed_document()
        (self.tmp / "sig.der").write_bytes(base64.b64decode(doc["signatures"][0]["value"]))
        (self.tmp / "content.bin").write_bytes(content)
        verify = subprocess.run(["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-noverify",
                                 "-in", str(self.tmp / "sig.der"), "-content", str(self.tmp / "content.bin"),
                                 "-out", "/dev/null"], capture_output=True, text=True)
        self.assertEqual(verify.returncode, 0, verify.stderr)
        printed = subprocess.run(["openssl", "cms", "-cmsout", "-print", "-inform", "DER",
                                  "-in", str(self.tmp / "sig.der")], capture_output=True, text=True).stdout
        for attr in ("contentType", "signingTime", "messageDigest", "signingCertificateV2"):
            self.assertIn(attr, printed)
        self.assertIn("1.2.840.113549.1.7.5", printed)  # content type the ETA expects
        # Tampering with the content breaks verification.
        (self.tmp / "content.bin").write_bytes(content.replace(b"9662", b"9663"))
        bad = subprocess.run(["openssl", "cms", "-verify", "-binary", "-inform", "DER", "-noverify",
                              "-in", str(self.tmp / "sig.der"), "-content", str(self.tmp / "content.bin"),
                              "-out", "/dev/null"], capture_output=True)
        self.assertNotEqual(bad.returncode, 0)

    def test_demo_eta_checks_signature_and_totals(self):
        eta = DemoETAClient(store=self.tmp / "eta.json")
        doc, _ = self.signed_document()
        ok = eta.submit_documents([doc])["acceptedDocuments"][0]
        self.assertEqual(eta.get_document_details(ok["uuid"])["status"], "Valid")
        doc["totalAmount"] = 1  # changed after signing
        bad = eta.submit_documents([doc])["acceptedDocuments"][0]
        details = eta.get_document_details(bad["uuid"])
        self.assertEqual(details["status"], "Invalid")
        self.assertTrue(details["validationResults"]["validationSteps"])


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_new_columns(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            conn = sqlite3.connect(tmp / "old.db")
            conn.executescript("""
                CREATE TABLE clients (id INTEGER PRIMARY KEY, accountant_id INTEGER, name TEXT, tax_id TEXT,
                    whatsapp_phone TEXT, eta_client_id TEXT, eta_client_secret TEXT, notes TEXT, created_at TEXT);
                CREATE TABLE invoices (id INTEGER PRIMARY KEY, client_id INTEGER, period TEXT, status TEXT,
                    created_at TEXT, updated_at TEXT, eta_uuid TEXT);
                CREATE TABLE parties (accountant_id INTEGER, tax_id TEXT, name TEXT, invoice_count INTEGER,
                    last_seen TEXT, PRIMARY KEY (accountant_id, tax_id));
            """)
            conn.close()
            Database(tmp / "old.db")
            conn = sqlite3.connect(tmp / "old.db")
            cols = {r[1] for r in conn.execute("PRAGMA table_info(invoices)")}
            conn.close()
            self.assertTrue({"eta_draft", "eta_state", "eta_check", "eta_long_id"} <= cols)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class IssuingFlowTests(AppTestCase):
    """Runs the app in ETA demo mode for these tests."""

    def setUp(self):
        self.service = self.app.state.service
        self._env, self._data = self.service.settings.eta_env, self.service.settings.data_dir
        self.service.settings.eta_env = "demo"
        self.service.settings.data_dir = Path(tempfile.mkdtemp())  # a fresh simulated ETA per test

    def tearDown(self):
        shutil.rmtree(self.service.settings.data_dir, ignore_errors=True)
        self.service.settings.eta_env, self.service.settings.data_dir = self._env, self._data

    def setup_client(self, email, profile=True):
        s = self.session(email)
        cid = self.create_client(s, "شركة الدلتا", "300112455")
        if profile:
            token = self.csrf(s, f"{self.base}/clients/{cid}")
            s.post(f"{self.base}/clients/{cid}/einvoice-settings", data={
                "csrf": token, "activity_code": "4690", "governate": "الإسكندرية", "region_city": "سموحة",
                "street": "فوزي معاذ", "building_number": "12", "item_type": "EGS",
                "item_code": "EG-300112455-1001", "unit_type": "EA", "signer": "token", "number_prefix": "INV-"})
        return s, cid

    def new_sale(self, s, cid):
        token = self.csrf(s, f"{self.base}/clients/{cid}")
        r = s.post(f"{self.base}/clients/{cid}/sales/new", data={"csrf": token}, allow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return int(r.headers["location"].split("/")[2])

    def form(self, token, action="submit", **over):
        data = {"csrf": token, "action": action, "internal_id": "INV-0001", "issue_date": date.today().isoformat(),
                "receiver_type": "B", "receiver_id": "488201736", "receiver_name": "شركة الأهرام",
                "receiver_governate": "القاهرة", "receiver_region_city": "مدينة نصر",
                "receiver_street": "مصطفى النحاس", "receiver_building_number": "7",
                "description": ["توريد مواسير", "تركيب"], "unit_type": ["EA", "HUR"], "quantity": ["20", "1"],
                "unit_price": ["350", "1,500"], "discount": ["200", ""], "vat_kind": ["standard", "standard"],
                "vat_rate": ["", ""], "item_code": ["", ""]}
        data.update(over)
        return data

    def test_issue_sale_invoice(self):
        s, cid = self.setup_client("issue@x.com")
        inv_id = self.new_sale(s, cid)
        page = s.get(f"{self.base}/invoices/{inv_id}/issue").text
        self.assertIn("INV-0001", page)  # number suggested from the client's prefix
        token = self.csrf(s, f"{self.base}/invoices/{inv_id}/issue")
        r = s.post(f"{self.base}/invoices/{inv_id}/issue", data=self.form(token))
        self.assertIn("اتصدرت على المنظومة", r.text)
        inv = self.db.get_invoice(inv_id)
        self.assertEqual((inv["eta_state"], inv["approved"], inv["direction"], inv["vat_amount"], inv["total"]),
                         ("valid", 1, "sale", 1162.0, 9462.0))
        self.assertTrue(inv["eta_uuid"])
        summary = self.service.summary(cid, PERIOD)
        self.assertEqual(summary["sales"]["vat"], 1162.0)
        # Issued invoices are locked.
        token = self.csrf(s, f"{self.base}/invoices/{inv_id}/issue")
        r = s.post(f"{self.base}/invoices/{inv_id}/issue", data=self.form(token, action="save", internal_id="X"))
        self.assertIn("مينفعش تتعدل", r.text)
        self.assertEqual(self.db.get_invoice(inv_id)["invoice_number"], "INV-0001")
        # The next new invoice gets the next number; the buyer's address is remembered.
        nxt = self.new_sale(s, cid)
        self.assertEqual(self.db.get_invoice(nxt)["invoice_number"], "INV-0002")
        self.assertEqual(self.db.party_details(self.db.get_client(cid)["accountant_id"], "488201736")["street"],
                         "مصطفى النحاس")

    def test_missing_settings_block_sending(self):
        s, cid = self.setup_client("issue-missing@x.com", profile=False)
        inv_id = self.new_sale(s, cid)
        token = self.csrf(s, f"{self.base}/invoices/{inv_id}/issue")
        r = s.post(f"{self.base}/invoices/{inv_id}/issue", data=self.form(token, receiver_building_number=""))
        self.assertIn("كود النشاط ناقص", r.text)
        self.assertIn("عنوان المشتري ناقص", r.text)
        inv = self.db.get_invoice(inv_id)
        self.assertIsNone(inv["eta_state"])
        self.assertEqual(inv["total"], 9462.0)  # the draft was still saved

    def test_paper_sale_prefills_editor_and_purchase_is_refused(self):
        s, cid = self.setup_client("issue-paper@x.com")
        self.upload(s, cid, "07_arabic.pdf")  # a purchase for this client
        purchase = self.db.list_invoices(cid)[0]
        r = s.get(f"{self.base}/invoices/{purchase['id']}/issue")
        self.assertIn("بيصدرها المورد", r.text)

    def test_purchase_matching_with_eta(self):
        s, cid = self.setup_client("match@x.com")
        self.upload(s, cid, "01_digital_ok.pdf", "07_arabic.pdf")
        invs = self.invoices(cid)
        nile = invs["01_digital_ok.pdf"]
        DemoETAClient(store=self.service.demo_store).add_received({
            "issuer": {"type": "B", "id": nile["supplier_tax_id"], "name": nile["supplier_name"]},
            "receiver": {"type": "B", "id": "300112455", "name": "شركة الدلتا"},
            "documentType": "I", "internalID": nile["invoice_number"],
            "dateTimeIssued": f"{nile['invoice_date']}T09:00:00Z",
            "netAmount": nile["subtotal"], "totalAmount": nile["total"],
            "taxTotals": [{"taxType": "T1", "amount": nile["vat_amount"]}], "invoiceLines": []})
        texts = []
        for period in sorted({i["period"] for i in invs.values()}):
            token = self.csrf(s, f"{self.base}/clients/{cid}")
            texts.append(s.post(f"{self.base}/clients/{cid}/eta-sync", data={"csrf": token, "period": period}).text)
        self.assertIn("اتربطت 1 فاتورة", " ".join(texts))
        invs = self.invoices(cid)
        page = s.get(f"{self.base}/clients/{cid}", params={"period": invs["07_arabic.pdf"]["period"]}).text
        self.assertIn("فاتورة مشتريات مش موجودة على المنظومة", page)
        self.assertEqual((invs["01_digital_ok.pdf"]["eta_check"], invs["01_digital_ok.pdf"]["approved"]),
                         ("matched", 1))
        self.assertTrue(invs["01_digital_ok.pdf"]["eta_uuid"])
        self.assertEqual(invs["07_arabic.pdf"]["eta_check"], "missing")
        self.assertNotIn("ETA ", " ".join(i["original_filename"] or "" for i in self.db.list_invoices(cid)))
        summary = self.service.summary(cid, invs["07_arabic.pdf"]["period"])
        self.assertEqual(summary["missing_on_eta"]["count"], 1)
        # A second sync changes nothing.
        token = self.csrf(s, f"{self.base}/clients/{cid}")
        s.post(f"{self.base}/clients/{cid}/eta-sync", data={"csrf": token, "period": nile["period"]})
        self.assertEqual(len(self.db.list_invoices(cid)), 2)


if __name__ == "__main__":
    unittest.main()
