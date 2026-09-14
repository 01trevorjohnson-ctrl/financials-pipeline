"""Robinhood Visa (...9669) export -- CSV or XLSX, columns:
Date, Time, Cardholder, Amount, Points, Balance, Status, Type, Merchant,
Description.

This is the one source format that already carries Time, Points and a
per-row running Balance (see the household's ``build_xlsx.py``, which used
this export's own column layout as the base for the whole workbook). Unlike
every other source, its Amount column already uses the household's sign
convention (money out = positive) as-is -- no sign flip needed.

Reconciliation strategy: this format has no separate "statement total" line,
but it DOES carry a running balance on every row, which is itself
effectively a printed, per-transaction checkpoint. We reconcile by checking
internal consistency of that running balance: for consecutive rows (sorted
oldest-first), balance[i] must equal balance[i-1] - amount[i] within
tolerance.
"""
from __future__ import annotations

import csv
import datetime
import io
import re

from .. import accounts
from .common import ParseResult, TxnRow, sniff_text, RECONCILE_TOLERANCE

ACCOUNT_NAME = accounts.ROBINHOOD_VISA
EXPECTED_COLUMNS = ['Date', 'Time', 'Cardholder', 'Amount', 'Points', 'Balance',
                    'Status', 'Type', 'Merchant', 'Description']


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'robinhood visa' in fn or '9669' in fn:
        return True
    if fn.endswith('.csv'):
        try:
            text = sniff_text(content, ('utf-8-sig',))
            header = text.splitlines()[0] if text.splitlines() else ''
            cols = [c.strip() for c in header.split(',')]
            return cols[:len(EXPECTED_COLUMNS)] == EXPECTED_COLUMNS
        except Exception:
            return False
    if fn.endswith('.xlsx'):
        try:
            rows = _read_xlsx_rows(content)
            return rows and [str(c).strip() for c in rows[0][:len(EXPECTED_COLUMNS)]] == EXPECTED_COLUMNS
        except Exception:
            return False
    return False


def _read_xlsx_rows(content: bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    ws = wb.active
    return list(ws.iter_rows(values_only=True))


def _read_csv_rows(content: bytes):
    text = sniff_text(content, ('utf-8-sig', 'cp1252'))
    return list(csv.reader(io.StringIO(text)))


def _to_date(v):
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    if v is None or str(v).strip() == '':
        return None
    return datetime.date.fromisoformat(str(v).strip()[:10])


def _to_float(v):
    if v is None or str(v).strip() == '':
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return float(str(v).replace(',', '').replace('$', '').strip())


def parse(content: bytes, filename: str) -> ParseResult:
    if filename.lower().endswith('.xlsx'):
        raw_rows = _read_xlsx_rows(content)
    else:
        raw_rows = _read_csv_rows(content)

    if not raw_rows:
        raise ValueError('Empty Robinhood Visa export')
    body = raw_rows[1:]

    parsed = []
    for r in body:
        if r is None or r[0] is None:
            continue
        d = _to_date(r[0])
        if d is None:
            continue
        amt = _to_float(r[3])
        if amt is None:
            continue
        parsed.append(dict(
            date=d, time=(str(r[1]).strip() if r[1] not in (None, '') else None),
            cardholder=(r[2] or None), amount=round(amt, 2),
            points=_to_float(r[4]), balance=_to_float(r[5]),
            status=(r[6] or 'Posted'), typ=(r[7] or 'Purchase'),
            merchant=re.sub(r'\s{2,}', ' ', str(r[8] or '').strip()),
            description=re.sub(r'\s{2,}', ' ', str(r[9] or '').strip()),
        ))

    parsed.sort(key=lambda r: (r['date'], r['time'] or ''))

    txns = [
        TxnRow(date=p['date'], time=p['time'], cardholder=p['cardholder'],
               amount=p['amount'], points=p['points'], balance=p['balance'],
               status=p['status'], type=p['typ'], merchant=p['merchant'],
               description=p['description'], card=ACCOUNT_NAME)
        for p in parsed
    ]

    with_balance = [p for p in parsed if p['balance'] is not None]
    mismatches = []
    for i in range(1, len(with_balance)):
        prev_bal = with_balance[i - 1]['balance']
        expected = round(prev_bal - with_balance[i]['amount'], 2)
        actual = with_balance[i]['balance']
        if abs(expected - actual) > RECONCILE_TOLERANCE:
            mismatches.append((with_balance[i]['date'], expected, actual))

    if not with_balance:
        ok, detail = False, 'No rows carried a Balance value to reconcile against'
    elif mismatches:
        ok = False
        d0, exp0, act0 = mismatches[0]
        detail = (f'{len(mismatches)} running-balance mismatches, first at {d0}: '
                  f'expected {exp0:.2f}, printed {act0:.2f}')
    else:
        ok = True
        detail = f'running balance internally consistent across {len(with_balance)} rows'

    close_date = max((t.date for t in txns), default=datetime.date.today())
    period_label = f'{min((t.date for t in txns), default=close_date)}..{close_date}'

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=period_label,
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
