"""Panama Mastercard (...2849 / ...3029) 'master' CSV export.

One CSV file (Spanish-locale, cp1252-encoded) covers a monthly bill for the
primary card (...2849) and its supplementary card (...3029); rows are
tagged to whichever account they belong to based on the "5536-XXXX-XXXX-NNNN"
summary line that precedes each block. Dates in this export are DD/MM/YYYY.

Ported from the household's reference script ``extract_new.py`` (the
"master CSV" section), with generic TOTAL ITBMS (Panama VAT) handling added
per ``category_keys.notes`` -- a statement-level VAT line with no offsetting
per-transaction detail should be booked as a single row dated to the
statement cutoff date.
"""
from __future__ import annotations

import datetime
import re

import accounts
from .common import num, clean_desc, ParseResult, TxnRow, csv_rows, RECONCILE_TOLERANCE

HOLDER = 'Sandra Viviana Suarez Jimenez'
SUBCARD_LINE_RE = re.compile(r'^5536-\d')
DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'panama mastercard' in fn or '2849' in fn or '3029' in fn:
        return True
    try:
        rows = csv_rows(content)
    except Exception:
        return False
    # NOTE: the generic "Date/.../Dollars" header is shared by BOTH this
    # format and the AMEX ConnectMiles master CSV (amex_connectmiles_csv.py)
    # -- it is not enough on its own to disambiguate the two when content
    # sniffing without a filename hint. The "5536-..." card-BIN prefix line
    # IS format-specific, so content-sniff relies on that alone.
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


def clean_merchant(d: str) -> str:
    d = clean_desc(d)
    d = re.sub(r'\s+5536\d*\*+\d+$', '', d)
    d = re.sub(r'\s+\d{6,}$', '', d)
    d = re.sub(r'\s+(PANAM[AÁ]?|BOGOTA|SAN FRANCISC|CIUDAD DE ME|STOCKHOLM)\s+[A-Z]$', '', d)
    d = re.sub(r'\s+-I-$| -I-$', '', d)
    d = re.sub(r'\s+[A-Z]$', '', d)
    return d.strip(' -')


def parse(content: bytes, filename: str) -> ParseResult:
    data = csv_rows(content)

    prev_bal = cutoff_bal = None
    subcard = '2849'
    in_txns = False
    grab_cutoff = False
    stmt_date = None
    rows = []          # (subcard, date, amount, type, desc, merchant)
    ssum = 0.0

    for r in data:
        r = [c.strip() for c in r]
        if not r:
            continue
        c0 = r[0]
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
            if len(r) > 2:
                d = pdate(r[2])
                if d:
                    stmt_date = d
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
        if d is None:
            continue
        if len(r) < 4:
            continue
        if stmt_date is None:
            stmt_date = d
        desc = clean_desc(r[1])
        amt = num(r[3])
        up = desc.upper()
        if 'SU PAGO RECIBIDO' in up or 'PAGO RECIBIDO GRACIAS' in up:
            typ = 'Payment'
        elif up.startswith('PROTECCION ROBO') or up.startswith('PLAN SALDOS DEU') \
                or up.startswith('SEGURO CYBER') or 'CARGOS X SERVICIO' in up:
            typ = 'Fee'
        elif amt < 0:
            typ = 'Credit'
        else:
            typ = 'Purchase'
        ssum += amt
        rows.append((subcard, d, round(amt, 2), typ, desc, clean_merchant(r[1])))

    if not rows:
        raise ValueError('No transaction rows found in Panama Mastercard master CSV')

    txns = [
        TxnRow(date=(d or max(rr[1] for rr in rows if rr[1])), amount=amt, type=typ,
               description=desc, merchant=merch, card=accounts.PANAMA_MC_BY_SUBCARD.get(sc, accounts.PANAMA_MASTERCARD_2849),
               cardholder=HOLDER)
        for (sc, d, amt, typ, desc, merch) in rows
    ]

    if prev_bal is not None and cutoff_bal is not None:
        exp = round(cutoff_bal - prev_bal, 2)
        diff = abs(prev_bal + ssum - cutoff_bal)
        ok = diff < RECONCILE_TOLERANCE
        detail = f'prev balance {prev_bal:.2f} + txns {ssum:.2f} = {prev_bal + ssum:.2f} vs cutoff {cutoff_bal:.2f}, diff {diff:.2f}'
    else:
        ok, detail = False, 'Could not find "Previous Balance" and/or cutoff balance on statement'

    close_date = stmt_date if stmt_date is not None else max(t.date for t in txns)
    accounts_covered = sorted(set(t.card for t in txns))

    return ParseResult(
        account_name=None, rows=txns, statement_period=close_date,
        period_label=str(close_date),
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=accounts_covered,
    )
