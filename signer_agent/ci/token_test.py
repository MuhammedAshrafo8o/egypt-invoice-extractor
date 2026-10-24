"""Runs on the Windows build machine against SoftHSM (a software token with a real PKCS#11 driver):
the same calls the program makes on an ePass2003 / eToken. Writes a short report for the release."""
import base64
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import p11  # noqa: E402

DLL = os.environ["SOFTHSM_DLL"]
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


def b(n: int) -> tuple:
    return tuple(n.to_bytes((n.bit_length() + 7) // 8 or 1, "big"))


def put_on_token(lib, label, key, cert, cka_id):
    """Initialise a SoftHSM token and put a key + certificate on it, all through PKCS#11."""
    import PyKCS11
    free = [sl for sl in lib.getSlotList(tokenPresent=True)
            if not (lib.getTokenInfo(sl).flags & PyKCS11.CKF_TOKEN_INITIALIZED)]
    lib.initToken(free[0], "5678", label)
    slot = [sl for sl in lib.getSlotList(tokenPresent=True) if lib.getTokenInfo(sl).label.strip() == label][0]
    s = lib.openSession(slot, PyKCS11.CKF_SERIAL_SESSION | PyKCS11.CKF_RW_SESSION)
    s.login("5678", user_type=PyKCS11.CKU_SO)
    s.initPin("1234")
    s.logout()
    s.login("1234")
    common = [(PyKCS11.CKA_CLASS, PyKCS11.CKO_PRIVATE_KEY), (PyKCS11.CKA_TOKEN, True), (PyKCS11.CKA_PRIVATE, True),
              (PyKCS11.CKA_SIGN, True), (PyKCS11.CKA_ID, tuple(cka_id)), (PyKCS11.CKA_LABEL, label)]
    if isinstance(key, rsa.RSAPrivateKey):
        n = key.private_numbers()
        s.createObject(common + [
            (PyKCS11.CKA_KEY_TYPE, PyKCS11.CKK_RSA), (PyKCS11.CKA_MODULUS, b(n.public_numbers.n)),
            (PyKCS11.CKA_PUBLIC_EXPONENT, b(n.public_numbers.e)), (PyKCS11.CKA_PRIVATE_EXPONENT, b(n.d)),
            (PyKCS11.CKA_PRIME_1, b(n.p)), (PyKCS11.CKA_PRIME_2, b(n.q)), (PyKCS11.CKA_EXPONENT_1, b(n.dmp1)),
            (PyKCS11.CKA_EXPONENT_2, b(n.dmq1)), (PyKCS11.CKA_COEFFICIENT, b(n.iqmp))])
    else:
        p256 = (0x06, 0x08, 0x2A, 0x86, 0x48, 0xCE, 0x3D, 0x03, 0x01, 0x07)
        s.createObject(common + [(PyKCS11.CKA_KEY_TYPE, PyKCS11.CKK_EC), (PyKCS11.CKA_EC_PARAMS, p256),
                                 (PyKCS11.CKA_VALUE, tuple(key.private_numbers().private_value.to_bytes(32, "big")))])
    s.createObject([(PyKCS11.CKA_CLASS, PyKCS11.CKO_CERTIFICATE), (PyKCS11.CKA_CERTIFICATE_TYPE, PyKCS11.CKC_X_509),
                    (PyKCS11.CKA_TOKEN, True), (PyKCS11.CKA_ID, tuple(cka_id)), (PyKCS11.CKA_LABEL, label),
                    (PyKCS11.CKA_VALUE, tuple(cert.public_bytes(serialization.Encoding.DER)))])
    s.logout()
    s.closeSession()


def setup():
    """Separate process: makes the two test tokens, keeps the keys to check signatures later."""
    import PyKCS11
    lib = PyKCS11.PyKCS11Lib()
    lib.load(DLL)
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ec_key = ec.generate_private_key(ec.SECP256R1())
    put_on_token(lib, "rsa", rsa_key, cert_for(rsa_key, "Test RSA Accountant"), b"\x01")
    put_on_token(lib, "ecc", ec_key, cert_for(ec_key, "Test ECC Accountant"), b"\x02")
    for name, key in (("rsa", rsa_key), ("ecc", ec_key)):
        with open(os.path.join(HERE, f"{name}.pem"), "wb") as f:
            f.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                      serialization.NoEncryption()))
    print("tokens ready")


def main():
    def load(name):
        with open(os.path.join(HERE, f"{name}.pem"), "rb") as f:
            return serialization.load_pem_private_key(f.read(), None)
    rsa_key, ec_key = load("rsa"), load("ecc")

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

    log("RESULT: " + ("PASS" if ok else "FAIL"))
    with open(os.path.join(HERE, "report.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(report) + "\n")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    setup() if "--setup" in sys.argv else main()
