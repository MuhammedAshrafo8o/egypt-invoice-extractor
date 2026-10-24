"""Runs on the Windows build machine against SoftHSM (a software token with a real PKCS#11 driver):
the same calls the program makes on an ePass2003 / eToken. Writes a short report for the release."""
import base64
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import p11  # noqa: E402

DLL = os.environ["SOFTHSM_DLL"]
UTIL = os.environ["SOFTHSM_UTIL"]
report = []


def log(line):
    print(line, flush=True)
    report.append(line)


def cert_for(key, cn):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=365)).sign(key, hashes.SHA256()))


def put_on_token(label, key, cert, cka_id):
    pem = os.path.join(HERE, f"{label}.pem")
    with open(pem, "wb") as f:
        f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()))
    subprocess.run([UTIL, "--init-token", "--free", "--label", label, "--pin", "1234", "--so-pin", "5678"], check=True)
    subprocess.run([UTIL, "--import", pem, "--token", label, "--label", label, "--id", cka_id.hex(),
                    "--pin", "1234"], check=True)
    import PyKCS11
    lib = PyKCS11.PyKCS11Lib()
    lib.load(DLL)
    for slot in lib.getSlotList(tokenPresent=True):
        if lib.getTokenInfo(slot).label.strip() == label:
            s = lib.openSession(slot, PyKCS11.CKF_SERIAL_SESSION | PyKCS11.CKF_RW_SESSION)
            s.login("1234")
            s.createObject([(PyKCS11.CKA_CLASS, PyKCS11.CKO_CERTIFICATE), (PyKCS11.CKA_CERTIFICATE_TYPE, PyKCS11.CKC_X_509),
                            (PyKCS11.CKA_TOKEN, True), (PyKCS11.CKA_ID, cka_id), (PyKCS11.CKA_LABEL, label),
                            (PyKCS11.CKA_VALUE, cert.public_bytes(serialization.Encoding.DER))])
            s.logout()
            s.closeSession()


def main():
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    put_on_token("rsa", rsa_key, cert_for(rsa_key, "Test RSA Accountant"), b"\x01")
    put_on_token("ecc", ec_key, cert_for(ec_key, "Test ECC Accountant"), b"\x02")

    tokens = p11.Tokens(DLL)
    certs = tokens.certificates()
    log(f"certificates found: {[c.name for c in certs]}")
    ok = len(certs) == 2
    data = b"daftar signed attributes"
    for c in certs:
        sig = tokens.sign(c, "1234", data)
        key = rsa_key if "RSA" in c.name else ec_key
        if isinstance(key, rsa.RSAPrivateKey):
            key.public_key().verify(sig, data, padding.PKCS1v15(), hashes.SHA256())
        else:
            key.public_key().verify(sig, data, ec.ECDSA(hashes.SHA256()))
        log(f"sign + verify OK: {c.name}")
    try:
        tokens.sign(certs[0], "9999", data)
        log("FAIL: wrong PIN accepted")
        ok = False
    except p11.TokenError as exc:
        log(f"wrong PIN message: {exc}")

    # the built .exe: is its server up and does it see the token?
    try:
        req = urllib.request.Request("http://127.0.0.1:47321/status",
                                     headers={"Origin": "https://daftar-blue.vercel.app"})
        with urllib.request.urlopen(req, timeout=5) as r:
            status = json.loads(r.read())
        log(f"exe /status: ready={status.get('ready')} certs={[c['name'] for c in status.get('certificates', [])]}"
            f" message={status.get('message')!r}")
    except Exception as exc:
        log(f"exe /status failed: {exc}")
        ok = False
    log("RESULT: " + ("PASS" if ok else "FAIL"))
    with open(os.path.join(HERE, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
