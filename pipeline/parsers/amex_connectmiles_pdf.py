"""AMEX ConnectMiles (...4473) PDF statement (BAC Credomatic account
3702-...-4474; cards ...4473 Sandra / ...4481 Trevor). Dates DD/MON/YYYY
with Spanish 3-letter month codes.

Ported from the household's reference script ``extract_amex.py``.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import ParseResult, TxnRow, pdf_text, RECONCILE_TOLERANCE

ACCOUNT_NAME = accounts.AMEX_CONNECTMILES
MON_ES3 = dict(ENE=1, FEB=2, MAR=3, ABR=4, MAY=5, JUN=6, JUL=7, AGO=8,
               SEP=9, OCT=10, NOV=11, DIC=12)
ROW = re.compile(r'^(\d{1,2})/([A-Z]{3})/(\d{4})\s+\S+\s+(.+?)\s+\$([\d,]*\.\d{2}|\.\d{2})(-?)\s*$')


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'amex' in fn or 'connectmiles' in fn:
        return True
    try:
        text = pdf_text(content)
    except Exception:
        return False
    return 'CONNECTMILES' in text.upper() or 'BAC CREDOMATIC' in text.upper()


def parse(content: bytes, filename: str) -> ParseResult:
    text = pdf_text(content)

    stmt = re.search(r'N.? estado de cuenta\s+(\d{6})', text)
    prev = re.search(r'Saldo Anterior\s+\$([\d,]+\.\d{2})', text)
    close = re.search(r'\bSALDO\s+\$([\d,]+\.\d{2})', text)
    prev_bal = float(prev.group(1).replace(',', '')) if prev else None
    close_bal = float(close.group(1).replace(',', '')) if close else None

    holder = 'Sandra Viviana Suarez Jimenez'
    subcard = '4473'
    rows = []
    ssum = 0.0
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            continue
        mc = re.match(r'^\*{6,}(\d{4})$', s)
        if mc:
            subcard = mc.group(1)
            holder = 'Trevor Johnson' if subcard == '4481' else 'Sandra Viviana Suarez Jimenez'
            continue
        if s.startswith('TREVOR STEVEN') or s.startswith('SANDRA VIVIANA'):
            continue
        m = ROW.match(s)
        if not m:
            continue
        dd, mon, yyyy = int(m.group(1)), MON_ES3.get(m.group(2)), int(m.group(3))
        if mon is None:
            continue
        desc = re.sub(r'\s+', ' ', m.group(4)).strip()
        amtxt = m.group(5)
        amt = float(('0' + amtxt if amtxt.startswith('.') else amtxt).replace(',', ''))
        if m.group(6) == '-':
            amt = -amt
        if amt == 0 or 'SOCIO COPA' in desc.upper():
            continue
        ssum += amt
        up = desc.upper()
        if 'PAGO RECIBIDO' in up:
            typ = 'Payment'
        elif amt < 0:
            typ = 'Credit'
        elif up.startswith(('PLAN SALDOS DEU', 'PROTECCION ROBO')):
            typ = 'Fee'
        else:
            typ = 'Purchase'
        merch = re.sub(r'\s*\(AX\)\s*$| -I-.*$| -XP-.*$|\(BOTON\).*$', '', desc).strip()
        rows.append((datetime.date(yyyy, mon, dd), round(amt, 2), typ, desc, merch, subcard, holder))

    if not rows:
        raise ValueError('No transaction rows found on AMEX ConnectMiles PDF statement')

    txns = [
        TxnRow(date=d, amount=amt, type=typ, description=desc, merchant=merch,
               card=ACCOUNT_NAME, cardholder=hold)
        for (d, amt, typ, desc, merch, _sc, hold) in rows
    ]

    if prev_bal is not None and close_bal is not None:
        calc = round(prev_bal + ssum, 2)
        diff = abs(calc - close_bal)
        ok = diff < RECONCILE_TOLERANCE
        detail = f'prev balance {prev_bal:.2f} + txns {ssum:.2f} = {calc:.2f} vs SALDO {close_bal:.2f}, diff {diff:.2f}'
    else:
        ok, detail = False, 'Could not find "Saldo Anterior" and/or "SALDO" on statement'

    close_date = max(t.date for t in txns)

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=(stmt.group(1) if stmt else str(close_date)),
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
