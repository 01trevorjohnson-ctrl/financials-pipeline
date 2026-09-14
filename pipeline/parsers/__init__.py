"""Parser registry + dispatch.

``detect_parser`` picks a parser module for a downloaded file by trying
filename-pattern hints first (mirroring the "Source Documents (Standardized
Names)" naming convention), then falling back to content sniffing (header
rows, PDF text markers) via each module's own ``matches()``. If nothing
matches, returns ``None`` -- the caller should then write a
``processed_statements`` row with ``status='needs_review'`` and a matching
``needs_review`` row, per the pipeline's documented policy of never
guessing at an unrecognized format.
"""
from __future__ import annotations

from . import (
    capital_one_card,
    citi,
    capital_one_360,
    huntington,
    robinhood_spending,
    robinhood_visa,
    panama_mastercard_csv,
    panama_debit_bac,
    banco_general_transfers,
    amex_connectmiles_pdf,
    amex_connectmiles_csv,
)
from .common import UnrecognizedStatementError, ParseResult, is_pdf

# Order matters for filename-hint matching: more specific patterns first.
# Each entry: (module, is_pdf_format: bool)
REGISTRY = [
    (citi, True),                      # AAdvantage / Costco
    (capital_one_card, True),          # Quicksilver
    (capital_one_360, True),           # 360 Checking + Savings
    (huntington, True),
    (amex_connectmiles_csv, False),
    (amex_connectmiles_pdf, True),
    (panama_mastercard_csv, False),
    (panama_debit_bac, False),
    (banco_general_transfers, True),
    (robinhood_visa, False),           # csv or xlsx
    (robinhood_spending, False),
]


def detect_parser(filename: str, content: bytes):
    """Return the parser module to use for this file, or None."""
    file_is_pdf = is_pdf(content)

    # Pass 1: filename hints, restricted to modules whose expected format
    # matches the actual file bytes (a ".pdf" that isn't really a PDF
    # shouldn't false-match a PDF-only parser purely on name).
    for module, wants_pdf in REGISTRY:
        if wants_pdf != file_is_pdf:
            continue
        try:
            if module.matches(filename, content):
                return module
        except Exception:
            continue

    # Pass 2: content sniffing only (ignore filename), still format-gated.
    for module, wants_pdf in REGISTRY:
        if wants_pdf != file_is_pdf:
            continue
        try:
            # matches() already does both name + content checks above; a
            # second pass adds nothing new here since it's the same
            # function. Kept as an explicit, separate step for clarity and
            # in case a module's matches() is later split into
            # name-only/content-only halves.
            if module.matches('', content):
                return module
        except Exception:
            continue

    return None


def parse_file(filename: str, content: bytes) -> ParseResult:
    module = detect_parser(filename, content)
    if module is None:
        raise UnrecognizedStatementError(
            f'"{filename}" did not match any known statement format (by filename or content sniff)')
    return module.parse(content, filename)
