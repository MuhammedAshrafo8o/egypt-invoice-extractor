"""The USB token, through its PKCS#11 driver (the DLL the token's setup installs on Windows).

Only two things are done with the token: list the certificates on it (no PIN needed), and sign
bytes with the private key of one of them (needs the PIN). Everything ETA-specific (the document,
the CMS envelope) is built by the Daftar server, so this program never needs to change when the
ETA changes its rules.
"""
from __future__ import annotations

import glob
import hashlib
import os
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from cryptography import x509
from cryptography.x509.oid import NameOID

# Drivers of the tokens sold in Egypt (Egypt Trust / MCDR / Fixed Solutions ...), most common first.
_WIN_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
KNOWN_DRIVERS = [
    "eps2003csp11.dll",        # ePass2003 — Egypt Trust, the most common e-invoice token
    "eps2003csp11v2.dll",
    "eTPKCS11.dll",            # SafeNet / Thales eToken
    "IDPrimePKCS11.dll",       # Gemalto IDPrime
    "WDPKCS.dll",              # WatchData
    "bit4ipki.dll",            # Bit4id
    "cmP11.dll",
    "aetpkss1.dll",            # Giesecke+Devrient / SafeSign
    "acospkcs11.dll",          # ACS
]


class TokenError(Exception):
    """Arabic message, shown as is in Daftar and in the program's window."""


@dataclass
class Certificate:
    key: str            # "<driver index>:<slot>:<id hex>", stable while the token stays in
    library: str
    slot: int
    cka_id: bytes
    der: bytes
    name: str
    valid_to: datetime

    @property
    def expired(self) -> bool:
        return self.valid_to < datetime.now(timezone.utc)


def find_drivers(configured: Optional[str] = None) -> list[str]:
    """Driver files on this PC. A configured path (settings.json) comes first."""
    found: list[str] = []
    if configured and os.path.isfile(configured):
        found.append(configured)
    extra = os.environ.get("DAFTAR_PKCS11")
    if extra and os.path.isfile(extra):
        found.append(extra)
    if sys.platform == "win32":
        for name in KNOWN_DRIVERS:
            path = os.path.join(_WIN_DIR, name)
            if os.path.isfile(path):
                found.append(path)
        found.extend(sorted(glob.glob(os.path.join(_WIN_DIR, "*pkcs11*.dll"))))   # other vendors' drivers
    seen, unique = set(), []
    for path in found:
        norm = os.path.normcase(os.path.abspath(path))
        if norm not in seen:
            seen.add(norm)
            unique.append(path)
    return unique


def _name(cert: x509.Certificate) -> str:
    for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
        values = cert.subject.get_attributes_for_oid(oid)
        if values:
            return str(values[0].value)
    return cert.subject.rfc4514_string()


class Tokens:
    """Loaded drivers, kept for the life of the program (loading a driver twice breaks some)."""

    def __init__(self, configured_driver: Optional[str] = None):
        self.configured_driver = configured_driver
        self._libs: dict[str, object] = {}
        self._lock = threading.Lock()   # one token operation at a time

    def _lib(self, path: str):
        import PyKCS11  # type: ignore
        lib = self._libs.get(path)
        if lib is None:
            lib = PyKCS11.PyKCS11Lib()
            lib.load(path)
            self._libs[path] = lib
        return lib

    def certificates(self) -> list[Certificate]:
        """Every certificate that has a private key, on every token plugged in."""
        try:
            import PyKCS11  # type: ignore
        except ImportError:
            raise TokenError("البرنامج ناقصه مكتبة PyKCS11.")
        drivers = find_drivers(self.configured_driver)
        if not drivers:
            raise TokenError("تعريف التوكن مش متسطب على الجهاز. سطّبه من الـ CD أو الموقع بتاع الشركة اللي اشتريت منها التوكن.")
        result: list[Certificate] = []
        with self._lock:
            for index, path in enumerate(drivers):
                try:
                    lib = self._lib(path)
                    slots = lib.getSlotList(tokenPresent=True)
                except Exception:
                    continue
                for slot in slots:
                    try:
                        session = lib.openSession(slot, PyKCS11.CKF_SERIAL_SESSION)
                    except Exception:
                        continue
                    try:
                        for obj in session.findObjects([(PyKCS11.CKA_CLASS, PyKCS11.CKO_CERTIFICATE)]):
                            der, cka_id = session.getAttributeValue(obj, [PyKCS11.CKA_VALUE, PyKCS11.CKA_ID], True)
                            der, cka_id = bytes(der), bytes(cka_id or b"")
                            try:
                                cert = x509.load_der_x509_certificate(der)
                            except ValueError:
                                continue
                            result.append(Certificate(
                                key=f"{index}:{slot}:{cka_id.hex()}", library=path, slot=slot, cka_id=cka_id,
                                der=der, name=_name(cert), valid_to=cert.not_valid_after_utc))
                    finally:
                        session.closeSession()
        # same certificate seen through two drivers: keep the first
        unique, seen = [], set()
        for c in result:
            if c.der not in seen:
                seen.add(c.der)
                unique.append(c)
        return unique

    def sign(self, cert: Certificate, pin: str, data: bytes) -> bytes:
        """SHA-256 signature over data with the certificate's private key (RSA PKCS#1 v1.5 or ECDSA, DER)."""
        import PyKCS11  # type: ignore
        with self._lock:
            lib = self._lib(cert.library)
            try:
                session = lib.openSession(cert.slot, PyKCS11.CKF_SERIAL_SESSION | PyKCS11.CKF_RW_SESSION)
            except PyKCS11.PyKCS11Error:
                raise TokenError("التوكن اتشال. دخّله تاني وجرّب.")
            try:
                try:
                    session.login(pin)
                except PyKCS11.PyKCS11Error as exc:
                    raise TokenError(_pin_message(exc))
                template = [(PyKCS11.CKA_CLASS, PyKCS11.CKO_PRIVATE_KEY)]
                if cert.cka_id:
                    template.append((PyKCS11.CKA_ID, cert.cka_id))
                keys = session.findObjects(template)
                if not keys:
                    raise TokenError("مالقيتش المفتاح بتاع الشهادة دي على التوكن.")
                key = keys[0]
                key_type = session.getAttributeValue(key, [PyKCS11.CKA_KEY_TYPE])[0]
                try:
                    if key_type == PyKCS11.CKK_EC:
                        raw = bytes(session.sign(key, hashlib.sha256(data).digest(),
                                                 PyKCS11.Mechanism(PyKCS11.CKM_ECDSA, None)))
                        return _ecdsa_der(raw)
                    return bytes(session.sign(key, data, PyKCS11.Mechanism(PyKCS11.CKM_SHA256_RSA_PKCS, None)))
                except PyKCS11.PyKCS11Error as exc:
                    raise TokenError(f"التوكن رفض يوقّع ({exc}).")
            finally:
                try:
                    session.logout()
                except Exception:
                    pass
                session.closeSession()


def _pin_message(exc) -> str:
    text = str(exc).upper()
    if "LOCKED" in text:
        return "التوكن اتقفل من كتر المحاولات الغلط. لازم يتفتح من الشركة اللي طلّعته."
    if "INCORRECT" in text or "LEN_RANGE" in text or "INVALID" in text:
        return "الرقم السري غلط. خلي بالك: التوكن بيتقفل بعد كام محاولة غلط."
    return f"ماقدرتش أفتح التوكن ({exc})."


def _ecdsa_der(raw: bytes) -> bytes:
    """r||s from the token -> DER SEQUENCE { INTEGER r, INTEGER s } (what CMS wants)."""
    half = len(raw) // 2

    def integer(b: bytes) -> bytes:
        b = b.lstrip(b"\x00") or b"\x00"
        if b[0] & 0x80:
            b = b"\x00" + b
        return b"\x02" + _len(len(b)) + b

    body = integer(raw[:half]) + integer(raw[half:])
    return b"\x30" + _len(len(body)) + body


def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    out = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(out)]) + out
