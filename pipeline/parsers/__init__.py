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
    """Turn any dropped-in file into a ParseResult, with as little human
    involvement as possible:

    1. A dedicated parser that matches and reconciles wins (free, exact).
    2. Otherwise -- no parser matches, the parser raised (layout changed),
       or its numbers don't reconcile -- the AI reader (``llm_extract``)
       reads the file. Its result is returned if it reconciles or carries
       no balances to check (booked as unverified).
    3. If the AI result doesn't add up either, the dedicated parser's
       result (if any) is returned so its own detail reaches Review;
       otherwise the AI's not-OK result.

    Raises UnrecognizedStatementError only when nothing usable came back.
    ``allow_llm=False`` skips the AI (main.py passes it for a file that
    already failed under the current AI version, so scheduled runs don't
    pay for the same failure again).
    """
    module = detect_parser(filename, content)
    dedicated, problem = None, None
    if module is not None:
        name = module.__name__.rsplit('.', 1)[-1]
        try:
            dedicated = module.parse(content, filename)
        except Exception as e:
            problem = f'{name} parser failed: {e}'
        else:
            if dedicated.reconciliation_ok:
                return dedicated
            problem = f'{name}: {dedicated.reconciliation_detail}'

    reason = problem or f'"{filename}" did not match any known statement format'
    if not llm_extract.is_configured() or not allow_llm:
        if dedicated is not None:
            return dedicated
        why = ('AI reader off: ANTHROPIC_API_KEY not set' if not llm_extract.is_configured()
               else f'{llm_extract.AI_FAILED_MARKER} on an earlier run, not retried')
        raise UnrecognizedStatementError(f'{reason} ({why})')

    try:
        ai = llm_extract.extract(filename, content)
    except llm_extract.ExtractionError as e:
        if dedicated is not None:
            dedicated.reconciliation_detail += f'; {llm_extract.AI_FAILED_MARKER}: {e}'
            return dedicated
        raise UnrecognizedStatementError(f'{reason}; {llm_extract.AI_FAILED_MARKER}: {e}') from e

    if problem:
        ai.reconciliation_detail += f' (used instead of {problem})'
    if ai.reconciliation_ok or dedicated is None:
        return ai
    dedicated.reconciliation_detail += f'; AI re-read did not add up either {llm_extract.VERSION_TAG}'
    return dedicated
