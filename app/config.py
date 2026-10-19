"""Settings, read from environment variables (and a .env file if present)."""
from __future__ import annotations

import os
import secrets
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # python-dotenv is optional
    pass

BASE_DIR = Path(__file__).resolve().parents[1]


def _bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    secret_key: str
    database_path: Path
    upload_dir: Path
    allow_signup: bool
    max_upload_mb: int
    use_ai: bool
    use_ocr: bool
    https_only_cookies: bool
    # WhatsApp Cloud API
    whatsapp_verify_token: str
    whatsapp_access_token: str
    whatsapp_phone_number_id: str
    whatsapp_app_secret: str
    whatsapp_api_version: str
    # Egyptian Tax Authority e-invoicing
    eta_env: str  # "preprod" or "prod"
    # Day of the month after the tax period when the VAT return is due (0 = last day of that month)
    vat_deadline_day: int = 0

    @property
    def whatsapp_enabled(self) -> bool:
        return bool(self.whatsapp_access_token and self.whatsapp_phone_number_id)


def load_settings() -> Settings:
    data_dir = Path(os.environ.get("DATA_DIR") or BASE_DIR / "data")
    if not data_dir.is_absolute():
        data_dir = BASE_DIR / data_dir
    secret = os.environ.get("SECRET_KEY")
    if not secret:
        # Persist a generated key so sessions survive restarts.
        key_file = data_dir / ".secret_key"
        data_dir.mkdir(parents=True, exist_ok=True)
        if key_file.exists():
            secret = key_file.read_text().strip()
        else:
            secret = secrets.token_urlsafe(48)
            key_file.write_text(secret)
    return Settings(
        secret_key=secret,
        database_path=Path(os.environ.get("DATABASE_PATH", data_dir / "app.db")),
        upload_dir=Path(os.environ.get("UPLOAD_DIR", data_dir / "uploads")),
        allow_signup=_bool("ALLOW_SIGNUP", True),
        max_upload_mb=int(os.environ.get("MAX_UPLOAD_MB", "15")),
        use_ai=_bool("USE_AI", True),
        use_ocr=_bool("USE_OCR", True),
        https_only_cookies=_bool("HTTPS_ONLY_COOKIES", False),
        whatsapp_verify_token=os.environ.get("WHATSAPP_VERIFY_TOKEN", ""),
        whatsapp_access_token=os.environ.get("WHATSAPP_ACCESS_TOKEN", ""),
        whatsapp_phone_number_id=os.environ.get("WHATSAPP_PHONE_NUMBER_ID", ""),
        whatsapp_app_secret=os.environ.get("WHATSAPP_APP_SECRET", ""),
        whatsapp_api_version=os.environ.get("WHATSAPP_API_VERSION", "v22.0"),
        eta_env=os.environ.get("ETA_ENV", "preprod"),
        vat_deadline_day=int(os.environ.get("VAT_DEADLINE_DAY", "0") or 0),
    )
