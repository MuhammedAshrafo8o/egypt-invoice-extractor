"""Extract and validate data from Egyptian invoices (PDF and images)."""
from .models import Invoice, Issue
from .pipeline import process_file, process_many, vat_summary
from .validation import ValidationConfig, validate_invoice

__all__ = [
    "Invoice", "Issue", "ValidationConfig",
    "process_file", "process_many", "validate_invoice", "vat_summary",
]
__version__ = "0.2.0"
