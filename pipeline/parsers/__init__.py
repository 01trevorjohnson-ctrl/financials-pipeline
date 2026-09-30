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
    panama_debit_bac_pdf,
    panama_debit_bac_transfers,
    banco_general_movimientos,
    amex_connectmiles_pdf,
    amex_connectmiles_csv,
)
from . import llm_extract
from .common import UnrecognizedStatementError, ParseResult, is_pdf

# Order matters for filename-hint matching: more specific patterns first.
# Each entry: (module, is_pdf_format: bool)
REGISTRY = [
    (citi, True),                      # AAdvantage / Costco
    (capital_one_card, True),          # Quicksilver
    (capital_one_360, True),           # 360 Checking + Savings
    (huntington, True),
    (panama_debit_bac_pdf, True),      # before amex_connectmiles_pdf: both are BAC PDFs
    (amex_connectmiles_csv, False),
    (amex_connectmiles_pdf, True),
    (panama_mastercard_csv, False),
    (panama_debit_bac_transfers, False),  # BAC "Consulta de Transferencias" (0794)
    (panama_debit_bac, False),
    (banco_general_movimientos, True), # savings "Últimos movimientos"
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


def parse_file(filename: str, content: bytes, allow_llm: bool = True) -> ParseResult:
    """Parse with the matching dedicated parser; if none matches, fall back
    to AI extraction (``llm_extract``), whose rows are only returned after
    the pipeline's own account and balance checks. ``allow_llm=False``
    skips the fallback (main.py passes it for a file the AI already failed
    on, so a scheduled run doesn't pay for the same failure again)."""
    module = detect_parser(filename, content)
    if module is not None:
        return module.parse(content, filename)
    reason = f'"{filename}" did not match any known statement format (by filename or content sniff)'
    if not llm_extract.is_configured():
        # Not a failure of the file: retried on every run until a key is set.
        raise UnrecognizedStatementError(f'{reason} (AI fallback off: ANTHROPIC_API_KEY not set)')
    if not allow_llm:
        raise UnrecognizedStatementError(
            f'{reason}; {llm_extract.AI_FAILED_MARKER} on an earlier run, not retried')
    try:
        return llm_extract.extract(filename, content)
    except llm_extract.ExtractionError as e:
        raise UnrecognizedStatementError(f'{reason}; {llm_extract.AI_FAILED_MARKER}: {e}') from e
