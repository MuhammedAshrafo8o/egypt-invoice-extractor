"""The signing program (signer_agent/) end to end, with a simulated token:
Daftar prepares -> the program signs (PIN asked once) -> Daftar assembles the CMS and submits."""
import base64
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "signer_agent"))

import p11  # noqa: E402
import server  # noqa: E402
from test_app import AppTestCase  # noqa: E402


def make_cert(key, cn, tax_id=""):
    attrs = [x509.NameAttribute(NameOID.COMMON_NAME, cn)]
    if tax_id:
        attrs.append(x509.NameAttribute(NameOID.ORGANIZATION_IDENTIFIER, tax_id))
    name = x509.Name(attrs)
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365)).sign(key, hashes.SHA256()))


class FakeTokens:
    """Stands in for p11.Tokens: same interface, key in memory, PIN 1234."""

    def __init__(self, key, cert):
        self.key, self.cert = key, cert
        self.der = cert.public_bytes(serialization.Encoding.DER)
        self.signed = 0

    def certificates(self):
        return [p11.Certificate(key="0:0:01", library="fake", slot=0, cka_id=b"\x01", der=self.der,
                                name=p11._name(self.cert), valid_to=self.cert.not_valid_after_utc)]

    def sign(self, cert, pin, data):
        if pin != "1234":
            raise p11.TokenError(p11._pin_message(Exception("CKR_PIN_INCORRECT")))
        self.signed += 1
        if isinstance(self.key, ec.EllipticCurvePrivateKey):
            return self.key.sign(data, ec.ECDSA(hashes.SHA256()))
        return self.key.sign(data, padding.PKCS1v15(), hashes.SHA256())


class AgentHTTPTests(unittest.TestCase):
    def setUp(self):
        self.appdata = tempfile.mkdtemp()
        self._env = os.environ.get("APPDATA")
        os.environ["APPDATA"] = self.appdata
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.tokens = FakeTokens(key, make_cert(key, "Mohamed Ashraf", ""))
        self.asked, self.pin_answers, self.approve = [], [("1234", True)], True
        self.agent = server.Agent(self.tokens, self._ask, lambda site: self.approve, lambda text: None)
        self.httpd = server.start_server(self.agent, port=0)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self._env is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = self._env
        shutil.rmtree(self.appdata, ignore_errors=True)

    def _ask(self, info):
        self.asked.append(info)
        return self.pin_answers.pop(0) if self.pin_answers else None

    def sign(self, origin, to_sign=b"hello", cert="0:0:01"):
        return requests.post(f"{self.url}/sign", headers={"Origin": origin}, data=json.dumps(
            {"cert": cert, "to_sign": base64.b64encode(to_sign).decode(), "summary": "فاتورة INV-1"})).json()

    def test_status_lists_certificate_for_daftar_site(self):
        r = requests.get(f"{self.url}/status", headers={"Origin": "https://daftar-blue.vercel.app"})
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://daftar-blue.vercel.app")
        data = r.json()
        self.assertTrue(data["ready"])
        self.assertEqual(data["certificates"][0]["name"], "Mohamed Ashraf")

    def test_preflight_allows_private_network(self):
        r = requests.options(f"{self.url}/sign", headers={
            "Origin": "https://daftar-blue.vercel.app", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Private-Network": "true"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(r.headers.get("Access-Control-Allow-Private-Network"), "true")

    def test_pin_asked_once_then_remembered(self):
        first = self.sign("https://daftar-blue.vercel.app", b"one")
        second = self.sign("https://daftar-blue.vercel.app", b"two")
        self.assertIn("signature", first)
        self.assertIn("signature", second)
        self.assertEqual(len(self.asked), 1)
        self.assertEqual(self.asked[0]["summary"], "فاتورة INV-1")
        self.tokens.key.public_key().verify(base64.b64decode(second["signature"]), b"two",
                                            padding.PKCS1v15(), hashes.SHA256())

    def test_wrong_pin_is_not_remembered(self):
        self.pin_answers = [("0000", True), ("1234", False)]
        bad = self.sign("https://daftar-blue.vercel.app")
        self.assertIn("الرقم السري غلط", bad["error"])
        good = self.sign("https://daftar-blue.vercel.app")
        self.assertIn("signature", good)
        self.assertEqual(len(self.asked), 2)

    def test_cancel(self):
        self.pin_answers = []
        self.assertIn("اتلغى", self.sign("https://daftar-blue.vercel.app")["error"])

    def test_unknown_site_needs_approval(self):
        r = requests.get(f"{self.url}/status", headers={"Origin": "https://evil.example"}).json()
        self.assertTrue(r.get("needs_approval"))
        self.assertEqual(r["certificates"], [])            # nothing leaks before approval
        self.approve = False
        r = requests.post(f"{self.url}/connect", headers={"Origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        self.approve = True
        r = requests.post(f"{self.url}/connect", headers={"Origin": "https://daftar.example.com"}).json()
        self.assertTrue(r["certificates"])
        saved = json.loads(Path(server.settings_path()).read_text(encoding="utf-8"))
        self.assertIn("https://daftar.example.com", saved["allowed_sites"])

    def test_plain_http_sites_refused(self):
        r = requests.post(f"{self.url}/sign", headers={"Origin": "http://evil.example"}, data="{}")
        self.assertEqual(r.status_code, 403)

    def test_ecdsa_raw_to_der(self):
        raw = bytes(range(1, 33)) + bytes([0x80] + [1] * 31)
        der = p11._ecdsa_der(raw)
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
        r, s = decode_dss_signature(der)
        self.assertEqual(r, int.from_bytes(raw[:32], "big"))
        self.assertEqual(s, int.from_bytes(raw[32:], "big"))


class RemoteIssueTests(AppTestCase):
    """Daftar (demo ETA) + the signing program: the PIN and the key never reach the server."""

    def setUp(self):
        self.service = self.app.state.service
        self._env, self._data = self.service.settings.eta_env, self.service.settings.data_dir
        self.service.settings.eta_env = "demo"
        self.service.settings.data_dir = Path(tempfile.mkdtemp())
        self.appdata = tempfile.mkdtemp()
        os.environ["APPDATA"] = self.appdata

    def tearDown(self):
        shutil.rmtree(self.service.settings.data_dir, ignore_errors=True)
        shutil.rmtree(self.appdata, ignore_errors=True)
        self.service.settings.eta_env, self.service.settings.data_dir = self._env, self._data

    def test_issue_through_signing_program(self):
        from test_einvoice import IssuingFlowTests
        flow = IssuingFlowTests
        s = self.session("agent@x.com")
        cid = self.create_client(s, "شركة الدلتا", "300112455")
        token = self.csrf(s, f"{self.base}/clients/{cid}")
        s.post(f"{self.base}/clients/{cid}/einvoice-settings", data={
            "csrf": token, "activity_code": "4690", "governate": "الإسكندرية", "region_city": "سموحة",
            "street": "فوزي معاذ", "building_number": "12", "item_type": "EGS", "item_code": "EG-300112455-1001",
            "unit_type": "EA", "signer": "agent", "signer_owner": "office", "number_prefix": "INV-"})
        r = s.post(f"{self.base}/clients/{cid}/sales/new", data={"csrf": self.csrf(s, f"{self.base}/clients/{cid}")},
                   allow_redirects=False)
        inv_id = int(r.headers["location"].split("/")[2])
        page = s.get(f"{self.base}/invoices/{inv_id}/issue").text
        self.assertIn("data-signing-program", page)

        # the program on the accountant's PC, with the office token (accountant's own certificate)
        key = ec.generate_private_key(ec.SECP256R1())
        tokens = FakeTokens(key, make_cert(key, "Mohamed Ashraf"))
        agent = server.Agent(tokens, lambda info: ("1234", True), lambda site: True, lambda t: None)
        httpd = server.start_server(agent, port=0)
        url = f"http://127.0.0.1:{httpd.server_address[1]}"
        try:
            status = requests.get(f"{url}/status", headers={"Origin": "http://localhost:8000"}).json()
            cert = status["certificates"][0]
            csrf = self.csrf(s, f"{self.base}/invoices/{inv_id}/issue")
            form = flow.form(self, csrf, action="prepare")
            form["cert"] = cert["der"]
            prepared = s.post(f"{self.base}/invoices/{inv_id}/issue", data=form).json()
            self.assertTrue(prepared["ok"], prepared)
            self.assertIn("INV-0001", prepared["summary"])
            signed = requests.post(f"{url}/sign", headers={"Origin": "http://localhost:8000"}, data=json.dumps(
                {"cert": cert["id"], "to_sign": prepared["to_sign"], "summary": prepared["summary"]})).json()
            self.assertIn("signature", signed)
            done = s.post(f"{self.base}/invoices/{inv_id}/issue", data={
                "csrf": csrf, "action": "finish", "ticket": prepared["ticket"], "signature": signed["signature"]}).json()
            self.assertTrue(done["ok"], done)
        finally:
            httpd.shutdown()
            httpd.server_close()
        inv = self.db.get_invoice(inv_id)
        self.assertEqual(inv["eta_state"], "valid")

    def test_tampered_signature_refused(self):
        s = self.session("agent2@x.com")
        cid = self.create_client(s, "شركة الدلتا", "300112455")
        token = self.csrf(s, f"{self.base}/clients/{cid}")
        s.post(f"{self.base}/clients/{cid}/einvoice-settings", data={
            "csrf": token, "activity_code": "4690", "governate": "الإسكندرية", "region_city": "سموحة",
            "street": "فوزي معاذ", "building_number": "12", "item_type": "EGS", "item_code": "EG-300112455-1001",
            "unit_type": "EA", "signer": "agent", "signer_owner": "office", "number_prefix": "INV-"})
        r = s.post(f"{self.base}/clients/{cid}/sales/new", data={"csrf": self.csrf(s, f"{self.base}/clients/{cid}")},
                   allow_redirects=False)
        inv_id = int(r.headers["location"].split("/")[2])
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        der = make_cert(key, "Someone").public_bytes(serialization.Encoding.DER)
        from test_einvoice import IssuingFlowTests
        csrf = self.csrf(s, f"{self.base}/invoices/{inv_id}/issue")
        form = IssuingFlowTests.form(self, csrf, action="prepare")
        form["cert"] = base64.b64encode(der).decode()
        prepared = s.post(f"{self.base}/invoices/{inv_id}/issue", data=form).json()
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        bad = other.sign(base64.b64decode(prepared["to_sign"]), padding.PKCS1v15(), hashes.SHA256())
        done = s.post(f"{self.base}/invoices/{inv_id}/issue", data={
            "csrf": csrf, "action": "finish", "ticket": prepared["ticket"],
            "signature": base64.b64encode(bad).decode()}).json()
        self.assertFalse(done["ok"])
        self.assertIsNone(self.db.get_invoice(inv_id)["eta_state"])


if __name__ == "__main__":
    unittest.main()
