"""Where uploaded invoice files are kept.

STORAGE=local (default): files on disk under UPLOAD_DIR; the stored reference is the file path.
STORAGE=db: files inside the database (table stored_files); the reference is "db:<id>/<name>".
            For serverless hosts (Vercel) that have no lasting disk.
"""
from __future__ import annotations

import hashlib
import mimetypes
import tempfile
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

DB_PREFIX = "db:"
PAGE_CACHE = Path(tempfile.gettempdir()) / "daftar-pages"


class FileStore:
    def __init__(self, settings, db):
        self.settings = settings
        self.db = db
        self.mode = getattr(settings, "storage", "local") or "local"

    # ------------------------------------------------------------ write
    def save(self, client_id: int, filename: str, content: bytes, period: str) -> str:
        ext = Path(filename or "").suffix.lower()
        if self.mode == "db":
            safe = f"{uuid.uuid4().hex}{ext}"
            file_id = self.db.save_blob(client_id, safe, content)
            return f"{DB_PREFIX}{file_id}/{safe}"
        folder = self.settings.upload_dir / str(client_id) / period
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{uuid.uuid4().hex}{ext}"
        path.write_bytes(content)
        return str(path)

    def delete(self, ref: Optional[str]) -> None:
        if not ref:
            return
        if ref.startswith(DB_PREFIX):
            self.db.delete_blob(self._blob_id(ref))
            for cached in PAGE_CACHE.glob(f"{self._cache_key(ref)}.page*.png"):
                cached.unlink(missing_ok=True)
            return
        path = Path(ref)
        for cached in path.parent.glob(f"{path.stem}.page*.png"):
            cached.unlink(missing_ok=True)
        path.unlink(missing_ok=True)

    # ------------------------------------------------------------- read
    def exists(self, ref: Optional[str]) -> bool:
        if not ref:
            return False
        if ref.startswith(DB_PREFIX):
            return self.db.blob_exists(self._blob_id(ref))
        path = Path(ref)
        return path.is_file() and self.settings.upload_dir.resolve() in path.resolve().parents

    def read(self, ref: str) -> bytes:
        if ref.startswith(DB_PREFIX):
            content = self.db.get_blob(self._blob_id(ref))
            if content is None:
                raise FileNotFoundError(ref)
            return bytes(content)
        return Path(ref).read_bytes()

    @contextmanager
    def local_path(self, ref: str) -> Iterator[Path]:
        """A real file path for the readers (PDF / OCR / AI). Database files go to a temp file."""
        if not ref.startswith(DB_PREFIX):
            yield Path(ref)
            return
        tmp = Path(tempfile.mkstemp(suffix=self.suffix(ref))[1])
        try:
            tmp.write_bytes(self.read(ref))
            yield tmp
        finally:
            tmp.unlink(missing_ok=True)

    def page_cache_path(self, ref: str, page: int) -> Path:
        if ref.startswith(DB_PREFIX):
            PAGE_CACHE.mkdir(parents=True, exist_ok=True)
            return PAGE_CACHE / f"{self._cache_key(ref)}.page{page}.png"
        path = Path(ref)
        return path.with_name(f"{path.stem}.page{page}.png")

    @staticmethod
    def suffix(ref: Optional[str]) -> str:
        return Path(ref or "").suffix.lower()

    @staticmethod
    def media_type(ref: str) -> str:
        return mimetypes.guess_type("x" + FileStore.suffix(ref))[0] or "application/octet-stream"

    # ---------------------------------------------------------- helpers
    @staticmethod
    def _blob_id(ref: str) -> int:
        return int(ref[len(DB_PREFIX):].split("/", 1)[0])

    @staticmethod
    def _cache_key(ref: str) -> str:
        return hashlib.sha1(ref.encode()).hexdigest()[:16]


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")
