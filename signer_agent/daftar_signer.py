"""دفتر — برنامج التوقيع

The one program the accountant installs on the PC the token is plugged into. Open it, leave it open,
and sign e-invoices from Daftar in the browser as usual. It shows one small window:
  * green: running and the token is in      * amber: put the token in / install its driver
and asks for the token PIN in its own box the first time it signs (can remember it until closed).
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import tkinter as tk
from tkinter import messagebox

from p11 import TokenError, Tokens
from server import PORT, VERSION, Agent, load_settings, start_server

GREEN, GREEN_SOFT, AMBER, AMBER_SOFT, INK, MUTED, PAPER = (
    "#134E43", "#E3F3E9", "#9A5F00", "#FDF1D8", "#18211E", "#7A8681", "#F4F6F3")
FONT = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def resource(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ------------------------------------------------------------------ start with Windows
def autostart_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, "DaftarSigner")
            return True
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, "DaftarSigner", 0, winreg.REG_SZ, f'"{sys.executable}" --background')
        else:
            try:
                winreg.DeleteValue(key, "DaftarSigner")
            except OSError:
                pass


def rtl_check(parent, text: str, var: tk.BooleanVar, bg: str, command=None, size: int = 10) -> tk.Frame:
    """Tk's checkbox puts its box on the left; for Arabic the box goes on the right of the text."""
    row = tk.Frame(parent, bg=bg)
    box = tk.Checkbutton(row, variable=var, command=command, bg=bg, activebackground=bg, bd=0)
    box.pack(side="right")
    label = tk.Label(row, text=text, bg=bg, font=(FONT, size), cursor="hand2")
    label.pack(side="right")

    def toggle(_=None):
        var.set(not var.get())
        if command:
            command()
    label.bind("<Button-1>", toggle)
    return row


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.calls: "queue.Queue" = queue.Queue()     # work the server threads need done on the UI thread
        settings = load_settings()
        self.tokens = Tokens(settings.get("pkcs11_library"))
        self.agent = Agent(self.tokens, self._ask_pin, self._approve_site, self._notify)
        self._build()
        self.root.after(100, self._pump)
        self._refresh()

    # ---------------------------------------------------------------- window
    def _build(self) -> None:
        r = self.root
        r.title("دفتر — برنامج التوقيع")
        r.configure(bg=PAPER)
        r.geometry("440x330")
        r.minsize(400, 300)
        try:
            r.iconbitmap(resource("daftar.ico"))
        except Exception:
            pass
        r.protocol("WM_DELETE_WINDOW", self._close)

        head = tk.Frame(r, bg=GREEN, padx=18, pady=14)
        head.pack(fill="x")
        tk.Label(head, text="دفتر — برنامج التوقيع", bg=GREEN, fg="white",
                 font=(FONT, 14, "bold"), anchor="e").pack(fill="x")
        tk.Label(head, text="سيبه مفتوح، ووقّع الفواتير من دفتر في المتصفح عادي.", bg=GREEN, fg="#D6E7E1",
                 font=(FONT, 10), anchor="e").pack(fill="x")

        body = tk.Frame(r, bg=PAPER, padx=18, pady=14)
        body.pack(fill="both", expand=True)
        self.status_box = tk.Frame(body, bg=AMBER_SOFT, padx=14, pady=12)
        self.status_box.pack(fill="x")
        self.status_title = tk.Label(self.status_box, text="بيدوّر على التوكن…", bg=AMBER_SOFT, fg=AMBER,
                                     font=(FONT, 12, "bold"), anchor="e", justify="right")
        self.status_title.pack(fill="x")
        self.status_text = tk.Label(self.status_box, text="", bg=AMBER_SOFT, fg=INK, font=(FONT, 10),
                                    anchor="e", justify="right", wraplength=370)
        self.status_text.pack(fill="x")

        self.last = tk.Label(body, text="", bg=PAPER, fg=MUTED, font=(FONT, 9), anchor="e", justify="right",
                             wraplength=390)
        self.last.pack(fill="x", pady=(12, 0))

        foot = tk.Frame(r, bg=PAPER, padx=18, pady=10)
        foot.pack(fill="x", side="bottom")
        self.auto = tk.BooleanVar(value=autostart_enabled())
        if sys.platform == "win32":
            rtl_check(foot, "يفتح لوحده لما الجهاز يشتغل", self.auto, PAPER, self._toggle_auto).pack(side="right")
        tk.Label(foot, text=f"نسخة {VERSION}", bg=PAPER, fg=MUTED, font=(FONT, 8)).pack(side="left")

    def _set_status(self, ok: bool, title: str, text: str) -> None:
        bg, fg = (GREEN_SOFT, GREEN) if ok else (AMBER_SOFT, AMBER)
        self.status_box.configure(bg=bg)
        self.status_title.configure(text=title, bg=bg, fg=fg)
        self.status_text.configure(text=text, bg=bg)

    def _refresh(self) -> None:
        def work():
            try:
                certs = self.tokens.certificates()
                error = ""
            except TokenError as exc:
                certs, error = [], str(exc)
            self.calls.put((self._show_certs, (certs, error), None))
        threading.Thread(target=work, daemon=True).start()
        self.root.after(4000, self._refresh)

    def _show_certs(self, certs, error) -> None:
        if error:
            self._set_status(False, "التوكن مش ظاهر", error)
        elif not certs:
            self._set_status(False, "حط التوكن في الجهاز", "البرنامج شغال، بس مفيش توكن متركب دلوقتي.")
        else:
            lines = []
            for c in certs:
                note = " — الشهادة منتهية!" if c.expired else f" — سارية لحد {c.valid_to:%d/%m/%Y}"
                lines.append(f"{c.name}{note}")
            self._set_status(True, "جاهز للتوقيع ✓", "\n".join(lines))

    def _toggle_auto(self) -> None:
        try:
            set_autostart(self.auto.get())
        except OSError as exc:
            messagebox.showerror("دفتر", f"ماقدرتش أغيّر الإعداد ده: {exc}")

    def _close(self) -> None:
        if messagebox.askyesno("دفتر", "لو قفلت البرنامج مش هتقدر توقّع فواتير من دفتر.\nتقفله؟"):
            self.root.destroy()

    # ------------------------------------------------- calls from the server threads
    def _pump(self) -> None:
        try:
            while True:
                fn, args, reply = self.calls.get_nowait()
                result = fn(*args)
                if reply is not None:
                    reply.put(result)
        except queue.Empty:
            pass
        self.root.after(100, self._pump)

    def _on_ui(self, fn, *args, timeout: float = 180):
        reply: "queue.Queue" = queue.Queue()
        self.calls.put((fn, args, reply))
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def _ask_pin(self, info: dict):
        return self._on_ui(self._pin_dialog, info)

    def _approve_site(self, site: str) -> bool:
        return bool(self._on_ui(self._site_dialog, site))

    def _notify(self, text: str) -> None:
        import time
        self.calls.put((lambda: self.last.configure(text=f"{time.strftime('%H:%M')}  {text}"), (), None))

    # ---------------------------------------------------------------- dialogs
    def _bring_front(self, win) -> None:
        self.root.deiconify()
        win.lift()
        win.attributes("-topmost", True)
        win.after(300, lambda: win.attributes("-topmost", False))
        win.focus_force()

    def _site_dialog(self, site: str) -> bool:
        self._bring_front(self.root)
        return messagebox.askyesno(
            "دفتر — موقع جديد",
            f"الموقع ده عاوز يوقّع فواتير بالتوكن بتاعك:\n\n{site}\n\nلو ده دفتر اللي بتشتغل عليه دوس «نعم».",
            parent=self.root)

    def _pin_dialog(self, info: dict):
        win = tk.Toplevel(self.root, bg="white", padx=20, pady=16)
        win.title("دفتر — توقيع فاتورة")
        try:
            win.iconbitmap(resource("daftar.ico"))
        except Exception:
            pass
        win.resizable(False, False)
        win.transient(self.root)
        result = {"value": None}

        def label(text, size=10, bold=False, fg=INK, pady=(0, 0)):
            tk.Label(win, text=text, bg="white", fg=fg, font=(FONT, size, "bold" if bold else "normal"),
                     anchor="e", justify="right", wraplength=360).pack(fill="x", pady=pady)

        label("توقيع فاتورة إلكترونية", 13, True, GREEN)
        label(info.get("summary") or "", 10, pady=(6, 0))
        label(f"بشهادة: {info.get('cert', '')}", 9, fg=MUTED, pady=(2, 0))
        label(f"من: {info.get('site', '')}", 9, fg=MUTED)
        label("الرقم السري للتوكن (PIN)", 10, True, pady=(14, 4))
        pin = tk.Entry(win, show="•", font=(FONT, 13), justify="center", relief="solid", bd=1)
        pin.pack(fill="x", ipady=5)
        remember = tk.BooleanVar(value=True)
        rtl_check(win, "افتكره لحد ما أقفل البرنامج", remember, "white", size=9).pack(anchor="e", pady=(8, 0))
        buttons = tk.Frame(win, bg="white")
        buttons.pack(fill="x", pady=(14, 0))

        def ok(_=None):
            if pin.get():
                result["value"] = (pin.get(), remember.get())
                win.destroy()

        tk.Button(buttons, text="وقّع", command=ok, bg=GREEN, fg="white", activebackground="#1C6456",
                  activeforeground="white", font=(FONT, 11, "bold"), relief="flat", padx=26, pady=6).pack(side="right")
        tk.Button(buttons, text="إلغاء", command=win.destroy, bg="white", font=(FONT, 10), relief="flat",
                  padx=16, pady=6).pack(side="right", padx=(0, 8))
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda _=None: win.destroy())
        self._bring_front(win)
        pin.focus_set()
        win.grab_set()
        self.root.wait_window(win)
        return result["value"]


def already_running() -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/status", timeout=2) as resp:
            return b"daftar-signer" in resp.read()
    except Exception:
        return False


def main() -> None:
    root = tk.Tk()
    try:
        app = App(root)
        start_server(app.agent)
    except OSError:
        root.withdraw()
        if already_running():
            messagebox.showinfo("دفتر", "برنامج التوقيع شغال بالفعل — هتلاقيه في شريط المهام تحت.")
        else:
            messagebox.showerror("دفتر", f"فيه برنامج تاني ماسك المنفذ {PORT}. اقفل البرامج المفتوحة وجرّب تاني.")
        root.destroy()
        return
    if "--background" in sys.argv:
        root.iconify()
    root.mainloop()


if __name__ == "__main__":
    main()
