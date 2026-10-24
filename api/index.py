"""Vercel entrypoint: the whole app is one ASGI function (see vercel.json)."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
# Serverless defaults (each can be overridden in the project's environment variables).
os.environ.setdefault("DATA_DIR", "/tmp/daftar")
os.environ.setdefault("STORAGE", "db")
os.environ.setdefault("SERVERLESS", "true")
os.environ.setdefault("USE_OCR", "false")       # no Tesseract binary on Vercel
os.environ.setdefault("HTTPS_ONLY_COOKIES", "true")

from app.main import create_app  # noqa: E402

app = create_app()
