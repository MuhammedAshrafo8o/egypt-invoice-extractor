"""Free, offline OCR with Tesseract (Arabic + English).

Used when no AI provider is configured (or the AI call fails). Accuracy on clean printed
invoices is reasonable; on handwriting, stamps and skewed phone photos it is weak, so OCR
results are always flagged for review.

Tesseract is a separate program (not a pip package):
  Windows : winget install UB-Mannheim.TesseractOCR
  macOS   : brew install tesseract
  Linux   : sudo apt install tesseract-ocr
Arabic language data: `python -m app setup-ocr` downloads it into ./tessdata.
"""
from __future__ import annotations

import functools
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from PIL import Image, ImageOps

PROJECT_TESSDATA = Path(__file__).resolve().parents[2] / "tessdata"
WINDOWS_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
LANGS = ("ara", "eng")


class OCRError(RuntimeError):
    pass


@functools.lru_cache(maxsize=1)
def tesseract_cmd() -> Optional[str]:
    configured = os.environ.get("TESSERACT_CMD")
    if configured and Path(configured).exists():
        return configured
    found = shutil.which("tesseract")
    if found:
        return found
    return next((p for p in WINDOWS_PATHS if Path(p).exists()), None)


def tessdata_dir() -> Optional[str]:
    configured = os.environ.get("TESSDATA_PREFIX")
    if configured:
        return configured
    if any(PROJECT_TESSDATA.glob("*.traineddata")):
        return str(PROJECT_TESSDATA)
    return None


def _base_cmd() -> list[str]:
    cmd = [tesseract_cmd() or "tesseract"]
    data = tessdata_dir()
    if data:
        cmd += ["--tessdata-dir", data]
    return cmd


@functools.lru_cache(maxsize=1)
def available_languages() -> tuple[str, ...]:
    if not tesseract_cmd():
        return ()
    try:
        out = subprocess.run(_base_cmd() + ["--list-langs"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return ()
    lines = (out.stdout + out.stderr).splitlines()
    return tuple(line.strip() for line in lines[1:] if line.strip() and " " not in line.strip())


def is_available() -> bool:
    return bool(set(LANGS) & set(available_languages()))


def status() -> str:
    """Human-readable state, for the dashboard and setup output."""
    if not tesseract_cmd():
        return "missing"
    langs = available_languages()
    if "ara" in langs:
        return "ok"
    return "no_arabic" if "eng" in langs else "missing"


def _prepare(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("L")
    # Tesseract works best around 300 DPI; phone photos of receipts are often small.
    if max(image.size) < 1800:
        scale = 1800 / max(image.size)
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.LANCZOS)
    return ImageOps.autocontrast(image, cutoff=1)


# Page-segmentation modes tried on every image. No single mode wins on all invoices:
# 4 keeps "label: value" lines together, 6/11 recover digits that 4 sometimes drops.
PSM_MODES = ("4", "6", "11")


def ocr_image(image: Image.Image, timeout: int = 90, psm: str = "4") -> str:
    if not is_available():
        raise OCRError("Tesseract is not installed")
    langs = "+".join(lang for lang in LANGS if lang in available_languages())
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    try:
        _prepare(image).save(tmp)
        # psm 4 = single column of variable-size lines: keeps "label: value" together on invoices.
        result = subprocess.run(
            _base_cmd() + [tmp, "stdout", "-l", langs, "--psm", psm, "-c", "preserve_interword_spaces=1"],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise OCRError("OCR timed out") from exc
    finally:
        Path(tmp).unlink(missing_ok=True)
    if result.returncode != 0:
        raise OCRError(result.stderr.decode("utf-8", "replace")[:300])
    return result.stdout.decode("utf-8", "replace")


def _pages(path: Path, max_pages: int) -> list[Image.Image]:
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        from ..pdfium_lock import PDFIUM_LOCK
        with PDFIUM_LOCK:
            pdf = pdfium.PdfDocument(str(path))
            try:
                return [pdf[i].render(scale=300 / 72).to_pil() for i in range(min(len(pdf), max_pages))]
            finally:
                pdf.close()
    with Image.open(path) as image:
        image.load()
        return [image.copy()]


def ocr_file(path: Path, max_pages: int = 3, psm: str = "4") -> str:
    return "\n".join(ocr_image(page, psm=psm) for page in _pages(Path(path), max_pages))


def ocr_passes(path: Path, max_pages: int = 3) -> list[str]:
    """One OCR text per page-segmentation mode; the caller keeps the most consistent one."""
    pages = _pages(Path(path), max_pages)
    texts = []
    for psm in PSM_MODES:
        try:
            texts.append("\n".join(ocr_image(page, psm=psm) for page in pages))
        except OCRError:
            continue
    if not texts:
        raise OCRError("All OCR passes failed")
    return texts
