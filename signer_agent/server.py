"""The little web server Daftar's page talks to, on this PC only (127.0.0.1).

GET  /status  -> is the program running, which certificates are on the token
POST /connect -> a site Daftar runs on that is not approved yet asks once (the program shows a yes/no)
POST /sign    -> {cert, to_sign (base64), summary} -> {signature (base64)}

Only sites the accountant approved can sign (Daftar's own site is approved out of the box). The
PIN never leaves this PC: it is typed in this program's window, never in the browser.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional
from urllib.parse import urlparse

from p11 import Certificate, TokenError, Tokens

VERSION = "1.0.0"
PORT = 47321
DEFAULT_SITES = ["https://daftar-blue.vercel.app"]
_LOCAL = re.compile(r"^http://(localhost|127\.0\.0\.1)(:\d+)?$")


def settings_path() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~/.config")
    return os.path.join(base, "DaftarSigner", "settings.json")


def load_settings() -> dict:
    try:
        with open(settings_path(), encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_settings(data: dict) -> None:
    path = settings_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


class Agent:
    """The program's state. The window plugs in the three callbacks (they run on the UI thread)."""

    def __init__(self, tokens: Tokens,
                 ask_pin: Callable[[dict], Optional[tuple[str, bool]]],
                 approve_site: Callable[[str], bool],
                 notify: Callable[[str], None]):
        self.tokens, self.ask_pin, self.approve_site, self.notify = tokens, ask_pin, approve_site, notify
        self.settings = load_settings()
        self._pins: dict[str, str] = {}          # remembered until the program closes, never on disk
        self._site_lock = threading.Lock()

    # ------------------------------------------------------------ sites
    def site_allowed(self, origin: str) -> bool:
        if not origin:
            return False
        if _LOCAL.match(origin):
            return True
        return origin in DEFAULT_SITES or origin in self.settings.get("allowed_sites", [])

    def ensure_site(self, origin: str) -> bool:
        if self.site_allowed(origin):
            return True
        if not origin.startswith("https://"):
            return False
        with self._site_lock:
            if self.site_allowed(origin):
                return True
            if not self.approve_site(urlparse(origin).netloc):
                return False
            self.settings.setdefault("allowed_sites", []).append(origin)
            save_settings(self.settings)
            return True

    # ------------------------------------------------------------ token
    def status(self) -> dict:
        try:
            certs = self.tokens.certificates()
        except TokenError as exc:
            return {"ready": False, "message": str(exc), "certificates": []}
        if not certs:
            return {"ready": False, "message": "حط التوكن في الجهاز.", "certificates": []}
        return {"ready": True, "message": "", "certificates": [
            {"id": c.key, "name": c.name, "valid_to": c.valid_to.strftime("%d/%m/%Y"), "expired": c.expired,
             "der": base64.b64encode(c.der).decode()} for c in certs]}

    def sign(self, origin: str, body: dict) -> dict:
        cert = self._find(body.get("cert", ""))
        try:
            data = base64.b64decode(body.get("to_sign", ""), validate=True)
        except Exception:
            data = b""
        if not data:
            raise TokenError("طلب التوقيع ناقص.")
        pin = self._pins.get(cert.key)
        if pin is None:
            answer = self.ask_pin({"site": urlparse(origin).netloc, "summary": str(body.get("summary", ""))[:300],
                                   "cert": cert.name})
            if not answer:
                raise TokenError("اتلغى التوقيع.")
            pin, remember = answer
            if remember:
                self._pins[cert.key] = pin
        try:
            signature = self.tokens.sign(cert, pin, data)
        except TokenError:
            self._pins.pop(cert.key, None)       # a wrong PIN is never retried on its own
            raise
        self.notify(f"اتوقّعت: {body.get('summary') or 'فاتورة'}")
        return {"signature": base64.b64encode(signature).decode()}

    def _find(self, key: str) -> Certificate:
        for cert in self.tokens.certificates():
            if cert.key == key:
                return cert
        raise TokenError("الشهادة دي مش موجودة على التوكن دلوقتي. اتأكد إن التوكن متركب.")


def make_handler(agent: Agent):
    class Handler(BaseHTTPRequestHandler):
        server_version = f"DaftarSigner/{VERSION}"

        def log_message(self, *args):   # quiet
            pass

        def _cors(self, origin: str, any_https: bool = False) -> None:
            if origin and (agent.site_allowed(origin) or (any_https and origin.startswith("https://"))):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Private-Network", "true")   # Chrome: public site -> this PC
            self.send_header("Access-Control-Max-Age", "600")

        def _json(self, code: int, payload: dict, origin: str, any_https: bool = False) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self._cors(origin, any_https)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            origin = self.headers.get("Origin", "")
            self.send_response(204)
            # unknown https sites get through the preflight so the accountant can approve them on /sign
            if origin.startswith("https://") or agent.site_allowed(origin):
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()

        def do_GET(self):
            origin = self.headers.get("Origin", "")
            if self.path.split("?")[0] != "/status":
                return self._json(404, {"error": "not found"}, origin)
            payload = {"app": "daftar-signer", "version": VERSION}
            if agent.site_allowed(origin):
                payload.update(agent.status())
            else:
                payload.update({"ready": True, "needs_approval": True, "certificates": []})
            self._json(200, payload, origin, any_https=True)

        def do_POST(self):
            origin = self.headers.get("Origin", "")
            path = self.path.split("?")[0]
            if path not in ("/sign", "/connect"):
                return self._json(404, {"error": "not found"}, origin)
            if not agent.ensure_site(origin):
                return self._json(403, {"error": "الموقع ده مش مسموح له يوقّع من التوكن."}, origin, any_https=True)
            if path == "/connect":   # a new site, just approved by the accountant
                return self._json(200, {"app": "daftar-signer", "version": VERSION, **agent.status()}, origin)
            try:
                length = min(int(self.headers.get("Content-Length") or 0), 1_000_000)
                body = json.loads(self.rfile.read(length) or b"{}")
                result = agent.sign(origin, body)
            except TokenError as exc:
                return self._json(200, {"error": str(exc)}, origin)
            except Exception as exc:  # never crash the program on a bad request
                return self._json(200, {"error": f"حصلت مشكلة في برنامج التوقيع: {exc}"}, origin)
            self._json(200, result, origin)

    return Handler


def start_server(agent: Agent, port: int = PORT) -> ThreadingHTTPServer:
    """Raises OSError when the port is taken (the program is already running)."""
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(agent))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


if __name__ == "__main__" and "--headless" in sys.argv:   # for testing without a window
    def _ask(info):
        pin = os.environ.get("DAFTAR_TEST_PIN")
        return (pin, True) if pin else None
    a = Agent(Tokens(), _ask, lambda site: True, print)
    start_server(a)
    print(f"listening on 127.0.0.1:{PORT}")
    threading.Event().wait()
