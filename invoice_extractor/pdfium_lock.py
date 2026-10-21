"""pypdfium2 (PDFium) is not thread-safe: every use goes through this lock.

Uploads are read by background workers while the dashboard renders PDF pages, so two
threads can touch PDFium at the same time; without the lock that crashes the process.
"""
import threading

PDFIUM_LOCK = threading.RLock()
