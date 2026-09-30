"""Shared helpers used by every statement parser.

Sign convention (matches the household's existing workbook, and the
``transactions.amount`` column in Supabase): money OUT / spending is
POSITIVE, money IN / received is NEGATIVE.

Every parser module exposes a single entrypoint:

    parse(content: bytes, filename: str) -> ParseResult

``ParseResult`` bundles the extracted transaction rows together with the
figures needed to reconcile against the statement's own printed totals.
``main.py`` is responsible for calling :func:`reconcile` (or trusting a
parser-supplied ``ok``/``detail`` pair) and refusing to insert anything for
a statement that does not reconcile within :data:`RECONCILE_TOLERANCE`.
"""
from __future__ import annotations

import datetime
import io
import re
from dataclasses import dataclass, field
from typing import Optional

RECONCILE_TOLERANCE = 0.02

MON = {m: i + 1 for i, m in enumerate(
    ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'])}

# Spanish 3-letter month abbreviations, used by Banco General statements.
MON_ES = {'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
          'jul': 7, 'ago': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dic': 12}

AMT_RE = r'-?\$[\d,]+\.\d{2}'


class ReconciliationError(Exception):
    """Raised by a parser when it cannot even attempt reconciliation
    (e.g. it could not find the statement's printed total at all)."""


class UnrecognizedStatementError(Exception):
    """Raised when a file cannot be matched to any known parser."""


def money(s: str) -> float:
    """Parse a "$1,234.56" / "-$1,234.56" / "1234.56-" style string."""
    s = (s or '').strip()
    if s in ('', '-'):
        return 0.0
    neg = s.startswith('-') or s.endswith('-')
    s = s.replace('$', '').replace(',', '').replace('+', '').strip()
    s = s.rstrip('-').lstrip('-').strip()
    v = float(s)
    return -v if neg else v


def num(s: str) -> float:
    """Parse a plain decimal string (no currency symbol), '' -> 0.0."""
    s = (s or '').strip().replace(',', '')
    if s in ('', '-'):
        return 0.0
    return float(s)


def clean_desc(s: str) -> str:
    s = (s or '').replace('\\', ' ')
    return re.sub(r'\s+', ' ', s).strip()


@dataclass
class TxnRow:
    """One parsed transaction, in the shape the DB layer expects.

    ``flow``/``category`` are deliberately left unset here -- they are
    assigned later by ``categorize.py``. ``type`` is the parser's own
    best read of the statement's own transaction type (Purchase / Payment /
    Credit / Fee / Interest / Adjustment) and IS set by the parser.
    """
    date: datetime.date
    amount: float
    type: str
    description: str
    merchant: str
    card: str                      # matches accounts.name exactly
    cardholder: Optional[str] = None
    time: Optional[str] = None
    points: Optional[float] = None
    balance: Optional[float] = None
    status: str = 'Posted'


@dataclass
class ParseResult:
    account_name: str              # single account name, OR None if the
                                    # statement covers >1 account (see rows)
    rows: list                     # list[TxnRow]
    statement_period: datetime.date  # date to store on processed_statements
                                      # (statement cutoff / closing date)
    period_label: str              # human string for logging/detail
    reconciliation_ok: bool
    reconciliation_detail: str
    accounts_covered: list = field(default_factory=list)  # distinct account
                                                            # names actually
                                                            # present in rows


def pdf_text(content: bytes) -> str:
    import pdfplumber
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return "\n".join(p.extract_text() or '' for p in pdf.pages)


def pdf_pages(content: bytes):
    import pdfplumber
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        return list(pdf.pages)


def is_pdf(content: bytes) -> bool:
    return content[:4] == b'%PDF'


def sniff_text(content: bytes, encoding_candidates=('utf-8-sig', 'cp1252', 'latin-1')) -> str:
    for enc in encoding_candidates:
        try:
            return content.decode(enc)
        except UnicodeDecodeError:
            continue
    return content.decode('utf-8', errors='replace')


def csv_rows(content: bytes, encoding_candidates=('cp1252', 'utf-8-sig', 'latin-1')):
    import csv
    text = sniff_text(content, encoding_candidates)
    return list(csv.reader(io.StringIO(text)))
