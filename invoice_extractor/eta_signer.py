"""CAdES-BES signature for ETA documents.

The ETA wants, for the issuer signature, a base64 CMS SignedData that is *detached*
(the content is not embedded) over the UTF-8 bytes of the serialized document, with
content type 1.2.840.113549.1.7.5 and these signed attributes:
    contentType, signingTime, messageDigest (SHA-256), signingCertificateV2 (ESS)
This is what the ETA reference signer produces. The DER is built by hand here so the
only dependency is `cryptography`.

Where the private key lives:
  * PfxSigner      — a .pfx / .p12 file (also used for tests and the demo mode)
  * Pkcs11Signer   — the USB token (ePass2003 / Egypt Trust / MCDR ...) through its
                     PKCS#11 library. Needs `pip install PyKCS11` on the accountant's PC.
                     Untested against a real token in this repo.
"""
from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa, ec
from cryptography.hazmat.primitives.serialization import pkcs12


class SigningError(RuntimeError):
    """Message is Arabic and safe to show to the accountant."""


# ------------------------------------------------------------------ tiny DER encoder
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def tlv(tag: int, content: bytes) -> bytes:
    return bytes([tag]) + _len(len(content)) + content


def seq(*items: bytes) -> bytes:
    return tlv(0x30, b"".join(items))


def set_of(*items: bytes) -> bytes:
    return tlv(0x31, b"".join(sorted(items)))  # DER: SET OF sorted by encoding


def integer(value: int) -> bytes:
    body = value.to_bytes(max(1, (value.bit_length() + 8) // 8), "big", signed=True)
    return tlv(0x02, body)


def octets(data: bytes) -> bytes:
    return tlv(0x04, data)


def oid(dotted: str) -> bytes:
    parts = [int(p) for p in dotted.split(".")]
    body = bytes([40 * parts[0] + parts[1]])
    for p in parts[2:]:
        chunk = [p & 0x7F]
        p >>= 7
        while p:
            chunk.append(0x80 | (p & 0x7F))
            p >>= 7
        body += bytes(reversed(chunk))
    return tlv(0x06, body)


def utc_time(moment: datetime) -> bytes:
    return tlv(0x17, moment.astimezone(timezone.utc).strftime("%y%m%d%H%M%SZ").encode())


def explicit(n: int, content: bytes) -> bytes:
    return tlv(0xA0 + n, content)


NULL = b"\x05\x00"
OID_SIGNED_DATA = "1.2.840.113549.1.7.2"
OID_DIGESTED_DATA = "1.2.840.113549.1.7.5"   # content type the ETA expects
OID_SHA256 = "2.16.840.1.101.3.4.2.1"
OID_RSA = "1.2.840.113549.1.1.1"
OID_ECDSA_SHA256 = "1.2.840.10045.4.3.2"
OID_CONTENT_TYPE = "1.2.840.113549.1.9.3"
OID_MESSAGE_DIGEST = "1.2.840.113549.1.9.4"
OID_SIGNING_TIME = "1.2.840.113549.1.9.5"
OID_SIGNING_CERT_V2 = "1.2.840.113549.1.9.16.2.47"


def _attribute(type_oid: str, value: bytes) -> bytes:
    return seq(oid(type_oid), set_of(value))


def signed_attributes(content: bytes, cert_der: bytes, moment: Optional[datetime] = None) -> list[bytes]:
    cert = x509.load_der_x509_certificate(cert_der)
    issuer_der = cert.issuer.public_bytes()
    ess_cert_id_v2 = seq(
        seq(oid(OID_SHA256)),                                   # hashAlgorithm (written explicitly)
        octets(hashlib.sha256(cert_der).digest()),              # certHash
        seq(seq(tlv(0xA4, issuer_der)), integer(cert.serial_number)),  # issuerSerial
    )
    return [
        _attribute(OID_CONTENT_TYPE, oid(OID_DIGESTED_DATA)),
        _attribute(OID_SIGNING_TIME, utc_time(moment or datetime.now(timezone.utc))),
        _attribute(OID_MESSAGE_DIGEST, octets(hashlib.sha256(content).digest())),
        _attribute(OID_SIGNING_CERT_V2, seq(seq(ess_cert_id_v2))),
    ]


def build_cms(content: bytes, signer: "Signer", moment: Optional[datetime] = None) -> bytes:
    cert_der = signer.certificate_der()
    cert = x509.load_der_x509_certificate(cert_der)
    attrs = signed_attributes(content, cert_der, moment)
    to_sign = set_of(*attrs)                      # signature covers the attrs encoded as SET (0x31)
    signature = signer.sign(to_sign)
    is_ec = isinstance(cert.public_key(), ec.EllipticCurvePublicKey)
    sig_alg = seq(oid(OID_ECDSA_SHA256)) if is_ec else seq(oid(OID_RSA), NULL)
    signer_info = seq(
        integer(1),
        seq(cert.issuer.public_bytes(), integer(cert.serial_number)),   # IssuerAndSerialNumber
        seq(oid(OID_SHA256), NULL),
        b"\xa0" + to_sign[1:],                                          # [0] IMPLICIT signedAttrs
        sig_alg,
        octets(signature),
    )
    signed_data = seq(
        integer(3),                                  # v3: eContentType is not id-data
        set_of(seq(oid(OID_SHA256), NULL)),
        seq(oid(OID_DIGESTED_DATA)),                 # detached: no eContent
        tlv(0xA0, cert_der),                         # [0] IMPLICIT certificates
        set_of(signer_info),
    )
    return seq(oid(OID_SIGNED_DATA), explicit(0, signed_data))


def sign_document_bytes(content: bytes, signer: "Signer", moment: Optional[datetime] = None) -> str:
    return base64.b64encode(build_cms(content, signer, moment)).decode("ascii")


# ------------------------------------------------------------------------ signers
class Signer:
    def certificate_der(self) -> bytes:
        raise NotImplementedError

    def sign(self, data: bytes) -> bytes:
        """Signature (SHA-256) over data."""
        raise NotImplementedError

    def certificate(self) -> x509.Certificate:
        return x509.load_der_x509_certificate(self.certificate_der())

    def describe(self) -> str:
        cert = self.certificate()
        return cert.subject.rfc4514_string()


class PfxSigner(Signer):
    def __init__(self, path: str | Path, password: str = ""):
        path = Path(path)
        if not path.exists():
            raise SigningError(f"ملف الشهادة مش موجود: {path}")
        try:
            key, cert, _ = pkcs12.load_key_and_certificates(path.read_bytes(),
                                                            password.encode() if password else None)
        except ValueError:
            raise SigningError("كلمة سر ملف الشهادة غلط، أو الملف مش شهادة PFX.")
        if key is None or cert is None:
            raise SigningError("ملف الشهادة مفيهوش مفتاح خاص أو شهادة.")
        self._key, self._cert = key, cert

    def certificate_der(self) -> bytes:
        return self._cert.public_bytes(serialization.Encoding.DER)

    def sign(self, data: bytes) -> bytes:
        if isinstance(self._key, rsa.RSAPrivateKey):
            return self._key.sign(data, padding.PKCS1v15(), hashes.SHA256())
        if isinstance(self._key, ec.EllipticCurvePrivateKey):
            return self._key.sign(data, ec.ECDSA(hashes.SHA256()))
        raise SigningError("نوع المفتاح في الشهادة مش مدعوم.")


class Pkcs11Signer(Signer):
    """USB token through its PKCS#11 library (e.g. eps2003csp11.dll for ePass2003)."""

    def __init__(self, library: str, pin: str, tax_id: str = ""):
        try:
            import PyKCS11  # type: ignore
        except ImportError:
            raise SigningError("التوقيع بالتوكن محتاج مكتبة PyKCS11 — شغّل: pip install PyKCS11")
        if not library or not Path(library).exists():
            raise SigningError("مكان مكتبة التوكن (PKCS#11) مش مظبوط — ظبطه من ETA_PKCS11_LIB في ملف .env.")
        self.P = PyKCS11
        lib = PyKCS11.PyKCS11Lib()
        lib.load(library)
        slots = lib.getSlotList(tokenPresent=True)
        if not slots:
            raise SigningError("مفيش توكن متوصل بالجهاز.")
        last_error = None
        for slot in slots:
            session = lib.openSession(slot, PyKCS11.CKF_SERIAL_SESSION)
            try:
                session.login(pin)
            except PyKCS11.PyKCS11Error as exc:
                last_error = exc
                session.closeSession()
                continue
            found = self._find(session, tax_id)
            if found:
                self.session, self._key, self._cert_der = session, found[0], found[1]
                return
            session.logout()
            session.closeSession()
        if last_error and "PIN" in str(last_error).upper():
            raise SigningError("الرقم السري للتوكن غلط.")
        raise SigningError(f"مالقيتش شهادة على التوكن فيها {tax_id}." if tax_id
                           else "مالقيتش شهادة توقيع على التوكن.")

    def _find(self, session, tax_id: str):
        P = self.P
        for cert_obj in session.findObjects([(P.CKA_CLASS, P.CKO_CERTIFICATE)]):
            der, cert_id = session.getAttributeValue(cert_obj, [P.CKA_VALUE, P.CKA_ID], True)
            der = bytes(der)
            cert = x509.load_der_x509_certificate(der)
            if tax_id and tax_id not in cert.subject.rfc4514_string():
                continue
            keys = session.findObjects([(P.CKA_CLASS, P.CKO_PRIVATE_KEY), (P.CKA_ID, cert_id)])
            if keys:
                return keys[0], der
        return None

    def certificate_der(self) -> bytes:
        return self._cert_der

    def sign(self, data: bytes) -> bytes:
        mech = self.P.Mechanism(self.P.CKM_SHA256_RSA_PKCS, None)
        return bytes(self.session.sign(self._key, data, mech))


def check_certificate(cert: x509.Certificate, tax_id: str, today: Optional[datetime] = None) -> list[str]:
    """Warnings (Arabic) about a certificate before using it."""
    today = today or datetime.now(timezone.utc)
    problems = []
    if cert.not_valid_after_utc < today:
        problems.append(f"الشهادة منتهية من {cert.not_valid_after_utc:%d/%m/%Y}.")
    if cert.not_valid_before_utc > today:
        problems.append("الشهادة لسه مابدأتش.")
    if tax_id and tax_id not in cert.subject.rfc4514_string():
        problems.append(f"الشهادة مش باسم الرقم الضريبي {tax_id} — المنظومة هترفض التوقيع.")
    return problems


def make_test_certificate(path: str | Path, tax_id: str, name: str, password: str = "") -> Path:
    """Self-signed PFX for the demo mode and tests. NOT accepted by the real ETA."""
    from datetime import timedelta
    from cryptography.x509.oid import NameOID
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "EG"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, name),
        x509.NameAttribute(NameOID.COMMON_NAME, f"{name} (test)"),
        x509.NameAttribute(NameOID.ORGANIZATION_IDENTIFIER, tax_id),
    ])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=730))
            .sign(key, hashes.SHA256()))
    enc = (serialization.BestAvailableEncryption(password.encode()) if password
           else serialization.NoEncryption())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"demo", key, cert, None, enc))
    return path
