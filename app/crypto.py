"""Encryption at rest for secrets the app has to read back (the clients' ETA Client Secret).

Key: ENCRYPTION_KEY in .env (a Fernet key), otherwise one generated in data/.encryption_key.
Keep a copy of that key somewhere safe and apart from the database backups: without it the
stored secrets cannot be read (they would have to be typed in again), and with it the database
alone is not enough to read them.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


def load_key(data_dir: Path) -> bytes:
    env = os.environ.get("ENCRYPTION_KEY", "").strip()
    if env:
        return env.encode()
    key_file = Path(data_dir) / ".encryption_key"
    if key_file.exists():
        return key_file.read_text().strip().encode()
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    key_file.write_bytes(key)
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    return key


class SecretBox:
    def __init__(self, key: bytes):
        self._fernet = Fernet(key)

    @staticmethod
    def is_encrypted(value: Optional[str]) -> bool:
        return bool(value) and value.startswith(PREFIX)

    def encrypt(self, value: Optional[str]) -> Optional[str]:
        if not value or self.is_encrypted(value):
            return value or None
        return PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        if not self.is_encrypted(value):
            return value  # stored before encryption existed
        try:
            return self._fernet.decrypt(value[len(PREFIX):].encode()).decode()
        except InvalidToken:
            raise ValueError("ماقدرناش نفك تشفير بيانات المنظومة — مفتاح التشفير (ENCRYPTION_KEY) اتغيّر. "
                             "دخّل Client Secret من جديد.")
