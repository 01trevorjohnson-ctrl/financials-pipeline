"""AMEX ConnectMiles (...4473) 'master' CSV export (BAC Credomatic, account
3702-...-4474; cards ...4473 Sandra / ...4481 Trevor / ...4447 small
account-level rebate credits, treated as Sandra's). Dates DD/MM/YYYY.

Ported from the household's reference scripts ``extract_amex_csv.py`` and
``extract_amex_may.py`` (the latter added TOTAL ITBMS handling, folded in
here per ``category_keys.notes``: a statement-level Panama VAT line with no
offsetting per-transaction detail is booked as a single row dated to the
statement cutoff date).
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import num, ParseResult, TxnRow, csv_rows, RECONCILE_TOLERANCE

ACCOUNT_NAME = accounts.AMEX_CONNECTMILES
SUBCARD_LINE_RE = re.compile(r'^3702-42\*\*-\*\*\*\*-\d{4}')
DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')
HOLDER = {'4473': 'Sandra Viviana Suarez Jimenez', '4481': 'Trevor Johnson',
          '4447': 'Sandra Viviana Suarez Jimenez'}


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'amex' in fn or 'connectmiles' in fn:
        return True
    try:
        rows = csv_rows(content)
    except Exception:
        return False
    # NOTE: the generic "Date/.../Dollars" header is shared by BOTH this
    # format and the Panama Mastercard master CSV
    # (panama_mastercard_csv.py) -- it is not enough on its own to
    # disambiguate the two when content sniffing without a filename hint.
    # The "3702-42**-****-..." card-BIN prefix line IS format-specific, so
    # content-sniff relies on that alone.
    for r in rows[:60]:
        for cell in r:
            if cell and SUBCARD_LINE_RE.search(cell):
                return True
    return False


def pdate(s: str):
    m = DATE_RE.match((s or '').strip())
    if not m:
        return None
    return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))  # DD/MM/YYYY


def parse(content: bytes, filename: str) -> ParseResult:
    data = csv_rows(content)

    prev_bal = cutoff_bal = None
    subcard = '4473'
    in_txns = False
    grab_cutoff = False
    stmt_date = None
    rows = []       # (subcard, date, amount, type, desc, merchant)
    ssum = 0.0

    for r in data:
        r = [c.strip() for c in r]
        if not r:
            continue
        c0 = r[0]
        if SUBCARD_LINE_RE.match(c0) and len(r) > 2:
            d = pdate(r[2])
            if d:
                stmt_date = d
        if c0 == 'Date' and len(r) > 3 and r[3] == 'Dollars':
            in_txns = True
            continue
        if grab_cutoff:
            if len(r) >= 4 and re.match(r'^-?\d', r[3] or ''):
                cutoff_bal = num(r[3])
            grab_cutoff = False
            continue
        if c0 == '' and len(r) > 1 and r[1].lower().startswith('previous balance'):
            prev_bal = num(r[3])
            continue
        if c0 == '' and len(r) > 1 and SUBCARD_LINE_RE.match(r[1]):
            m = re.search(r'(\d{4})\s*$', r[1])
            if m:
                subcard = m.group(1)
            continue
        if c0 == '' and len(r) > 1 and 'TOTAL ITBMS' in r[1].upper():
            mm = re.search(r'\$?([\d,]*\.\d{2})', r[1])
            if mm and mm.group(1):
                itbms = float(mm.group(1).replace(',', ''))
                if itbms:
                    ssum += itbms
                    rows.append((subcard, stmt_date, round(itbms, 2), 'Fee',
                                 'TOTAL ITBMS (Panama VAT, statement total)', 'ITBMS (Panama VAT)'))
            continue
        if c0.startswith('CURRENT Interest MONTH'):
            grab_cutoff = True
            in_txns = False
            continue
        if not in_txns:
            continue
        d = pdate(c0)
        if d is None or len(r) < 4:
            continue
        if stmt_date is None:
            stmt_date = d
        desc = re.sub(r'\s+', ' ', r[1]).strip()
        amt = num(r[3])
        up = desc.upper()
        if 'SOCIO COPA' in up or amt == 0:
            continue
        ssum += amt
        if 'SU PAGO RECIBIDO' in up:
            typ = 'Payment'
        elif amt < 0:
            typ = 'Credit'
        elif up.startswith(('PLAN SALDOS DEU', 'PROTECCION ROBO')):
            typ = 'Fee'
        else:
            typ = 'Purchase'
        merch = re.sub(r'\s*\(AX\)\s*$| -I-.*$', '', desc).strip()
        rows.append((subcard, d, round(amt, 2), typ, desc, merch))

    if not rows:
        raise ValueError('No transaction rows found in AMEX ConnectMiles master CSV')

    txns = [
        TxnRow(date=(d or stmt_date or max(rr[1] for rr in rows if rr[1])),
               amount=amt, type=typ, description=desc, merchant=merch,
               card=ACCOUNT_NAME, cardholder=HOLDER.get(sc, 'Sandra Viviana Suarez Jimenez'))
        for (sc, d, amt, typ, desc, merch) in rows
    ]

    if prev_bal is not None and cutoff_bal is not None:
        diff = abs(prev_bal + ssum - cutoff_bal)
        ok = diff < RECONCILE_TOLERANCE
        detail = f'prev balance {prev_bal:.2f} + txns {ssum:.2f} = {prev_bal + ssum:.2f} vs cutoff {cutoff_bal:.2f}, diff {diff:.2f}'
    else:
        ok, detail = False, 'Could not find "Previous Balance" and/or cutoff balance on statement'

    close_date = stmt_date if stmt_date is not None else max(t.date for t in txns)

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=str(close_date),
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
