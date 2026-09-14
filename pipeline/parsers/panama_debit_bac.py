"""Panama debit account (...0794) CSV export ("Fecha de Transaccion..."
header), BAC Credomatic. Dates DD/MM/YYYY, columns: Fecha, Referencia,
Codigo, Descripcion, Debito, Credito, Balance.

Ported from the household's reference script ``extract_new.py`` (the debit
CSV section). The reference script's reconciliation used hardcoded totals
specific to the one file the household had already processed by hand; for
a general, repeatable pipeline we instead reconcile against the file's own
running Balance column (internal consistency: balance[i] must equal
balance[i-1] - debito[i] + credito[i]), which generalizes to any new file.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import num, clean_desc, ParseResult, TxnRow, csv_rows, RECONCILE_TOLERANCE

ACCOUNT_NAME = accounts.PANAMA_DEBIT
HOLDER = 'Sandra Viviana Suarez Jimenez'
DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')
HEADER_PREFIX = 'Fecha de Transacci'


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'bac' in fn or 'debit' in fn or '0794' in fn:
        return True
    try:
        rows = csv_rows(content)
    except Exception:
        return False
    return any(r and r[0].startswith(HEADER_PREFIX) for r in rows[:20])


def clean_merchant(desc: str) -> str:
    d = clean_desc(desc)
    d = re.sub(r'\s+5536\d*\*+\d+$', '', d)
    d = re.sub(r'\s+\d{6,}$', '', d)
    d = re.sub(r'\s+(PANAM[AÁ]?|BOGOTA|SAN FRANCISC|CIUDAD DE ME|STOCKHOLM)\s+[A-Z]$', '', d)
    d = re.sub(r'\s+-I-$| -I-$', '', d)
    d = re.sub(r'\s+[A-Z]$', '', d)
    return d.strip(' -')


def pdate(s: str):
    m = DATE_RE.match((s or '').strip())
    if not m:
        return None
    return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))  # DD/MM/YYYY


def parse(content: bytes, filename: str) -> ParseResult:
    data = csv_rows(content)

    in_txns = False
    rows = []       # (date, amount, type, desc, merchant, balance)
    for r in data:
        r = [c.strip() for c in r]
        if not r:
            continue
        if r[0].startswith(HEADER_PREFIX):
            in_txns = True
            continue
        if not in_txns:
            continue
        d = pdate(r[0])
        if d is None:
            continue
        if len(r) < 7:
            continue
        ref, code, desc = r[1], r[2], clean_desc(r[3])
        debito, credito, bal = num(r[4]), num(r[5]), num(r[6])
        up = desc.upper()
        if credito > 0:
            amt = -credito
            typ = 'Interest' if 'INTERES' in up else 'Credit'
        else:
            amt = debito
            typ = 'Purchase' if code == 'CP' else 'Payment'
        full_desc = (f'[{code}] ' + desc) if code else desc
        rows.append((d, round(amt, 2), typ, full_desc, clean_merchant(desc), round(bal, 2)))

    if not rows:
        raise ValueError('No transaction rows found in Panama debit account CSV (expected header starting '
                          f'"{HEADER_PREFIX}")')

    txns = [
        TxnRow(date=d, amount=amt, type=typ, description=desc, merchant=merch,
               card=ACCOUNT_NAME, cardholder=HOLDER, balance=bal)
        for (d, amt, typ, desc, merch, bal) in rows
    ]

    # Reconcile against the file's own running balance: balance[i] should
    # equal balance[i-1] - amount[i] (amount already uses out=+/in=- sign).
    mismatches = []
    for i in range(1, len(rows)):
        prev_bal = rows[i - 1][5]
        expected = round(prev_bal - rows[i][1], 2)
        actual = rows[i][5]
        if abs(expected - actual) > RECONCILE_TOLERANCE:
            mismatches.append((rows[i][0], expected, actual))

    if mismatches:
        ok = False
        d0, exp0, act0 = mismatches[0]
        detail = f'{len(mismatches)} running-balance mismatches, first at {d0}: expected {exp0:.2f}, printed {act0:.2f}'
    else:
        ok = True
        detail = f'running balance internally consistent across {len(rows)} rows'

    close_date = max(t.date for t in txns)

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=f'{min(t.date for t in txns)}..{close_date}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
