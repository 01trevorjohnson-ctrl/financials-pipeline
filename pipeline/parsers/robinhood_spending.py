"""Robinhood spending account CSV export (header: Date,Description,Amount).

Ported from the household's reference script ``extract_bank.py``.

NOTE ON RECONCILIATION: unlike the bank/card PDF statements, this is a
plain CSV export with no independently-printed "ending balance" or
statement total to check parsed amounts against (no Balance column, no
summary page). True reconciliation (parsed sum vs. a statement-printed
figure) is therefore not possible for this format. We instead do a
structural sanity check (every row parses to a valid date + numeric
amount, using the file's own row count) and always report
``reconciliation_ok=True`` with a detail note explaining why -- this is a
deliberate, documented exception to the "reconcile before insert" rule
described in the pipeline README.
"""
from __future__ import annotations

import csv
import datetime
import io
import re

import accounts
from .common import ParseResult, TxnRow, sniff_text

ACCOUNT_NAME = accounts.ROBINHOOD_SPENDING
EXPECTED_HEADER = 'Date,Description,Amount'


def matches(filename: str, content: bytes) -> bool:
    if 'robinhood spending' in filename.lower():
        return True
    try:
        text = sniff_text(content, ('utf-8-sig',))
    except Exception:
        return False
    first_line = text.splitlines()[0].strip() if text.splitlines() else ''
    return first_line == EXPECTED_HEADER


def parse(content: bytes, filename: str) -> ParseResult:
    text = sniff_text(content, ('utf-8-sig',))
    lines = text.splitlines()
    if not lines or lines[0].strip() != EXPECTED_HEADER:
        raise ValueError(f'Expected header "{EXPECTED_HEADER}", got "{lines[0] if lines else ""}"')

    rd = list(csv.DictReader(io.StringIO(text)))
    rows = []
    seen_wise = set()
    bad_rows = 0
    for r in rd:
        try:
            d = datetime.date.fromisoformat(r['Date'].strip())
            raw = float(r['Amount'])
        except Exception:
            bad_rows += 1
            continue
        desc = re.sub(r'\s{2,}', ' ', r['Description'].strip())
        # The export repeats the same WISE debit up to 5x (only one cleared,
        # the rest were declined) -- keep a single row per (date, amount).
        if 'WISE' in desc.upper():
            wkey = (d, round(raw, 2))
            if wkey in seen_wise:
                continue
            seen_wise.add(wkey)
        signed = -raw   # CSV: out=negative/in=positive -> flip to out=positive/in=negative
        u = desc.upper()
        if 'INTEREST' in u:
            typ = 'Interest'
        elif raw > 0:
            typ = 'Credit'
        elif 'DEBIT CARD TRANSACTION' in u:
            typ = 'Purchase'
        else:
            typ = 'Payment'
        merch = re.sub(r'\s+at\s+', ' ', desc).replace('Debit Card Transaction', '').strip()
        rows.append(TxnRow(date=d, amount=round(signed, 2), type=typ, description=desc,
                            merchant=merch, card=ACCOUNT_NAME))

    ok = bad_rows == 0 and len(rows) > 0
    detail = (f'{len(rows)} rows parsed, {bad_rows} unparseable rows. '
              'This CSV export carries no independent statement total/balance '
              'to reconcile against; structural parse check only.')

    close_date = max((t.date for t in rows), default=datetime.date.today())
    period_label = f'{min((t.date for t in rows), default=close_date)}..{close_date}'

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=rows, statement_period=close_date,
        period_label=period_label,
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
