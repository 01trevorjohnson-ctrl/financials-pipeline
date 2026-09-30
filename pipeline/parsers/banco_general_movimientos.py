"""Banco General (Panama) savings account "Últimos movimientos" PDF export
(Banca en Línea / Banca Móvil download, typically named
``ULTIMOS-MOVIMIENTOS-CUENTA-DE-AHORROS-YYYY-MM-DD.pdf``).

This is the household's only Banco General source: it lists every
movement on the account (Yappy payments, Banca Móvil transfers, deposits,
fees), superseding the older outgoing-transfers-only "Transacciones
realizadas" PDF. Rows keep the existing ``BANCO_GENERAL_TRANSFERS`` account
name so history and statement coverage stay continuous.

Layout (one pdfplumber table per page, newest row first)::

    Fecha        Descripción                              Monto     Saldo total
    27-sep-2026  PAGO YAPPY BG A Parroquia San Lucas ...  -$50.00   $1,315.49

``Monto`` is signed from the account's point of view (negative = money
out); we flip it to the pipeline's convention (money OUT = positive).

RECONCILIATION: every row carries the running balance, so we check the
chain balance[newer] == balance[older] + monto[newer] across the whole
file, plus the header's printed "Saldo total" against the newest row's
balance. Any break fails the file (nothing inserted).

OVERLAP: this is a rolling "last N movements" export, not a closed
statement period, so consecutive downloads overlap each other (and the
historical transfers backfill). The result sets ``dedupe_against_ledger``
so ``main.py`` skips rows already in the ledger for this account.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import (MON_ES, RECONCILE_TOLERANCE, ParseResult, TxnRow, clean_desc, money,
                     pdf_pages, pdf_text)

ACCOUNT_NAME = accounts.BANCO_GENERAL_TRANSFERS
DATE_RE = re.compile(r'^(\d{2})-([a-z]{3})-(\d{4})$')
HEADER = ['fecha', 'descripción', 'monto', 'saldo total']
SALDO_RE = re.compile(r'Saldo total\s+(-?\$[\d,]+\.\d{2})')
HOLDER_RE = re.compile(r'^Titular(?:e|es)?\s+(.+)$', re.MULTILINE)


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'movimientos' in fn or 'banco general' in fn:
        return True
    try:
        text = pdf_text(content)
    except Exception:
        return False
    return 'Banco General' in text and 'Fecha Descripción Monto Saldo total' in text


def pdate(s: str):
    m = DATE_RE.match((s or '').strip().lower())
    if not m or m.group(2) not in MON_ES:
        return None
    return datetime.date(int(m.group(3)), MON_ES[m.group(2)], int(m.group(1)))


def txn_type(desc: str, monto: float) -> str:
    d = desc.upper()
    if 'INTERES' in d:
        return 'Interest'
    if 'COMISION' in d or 'CARGO' in d or 'ITBMS' in d:
        return 'Fee'
    return 'Credit' if monto > 0 else 'Purchase'


def clean_merchant(desc: str) -> str:
    d = clean_desc(desc)
    d = re.sub(r'^(PAGO\s+)?YAPPY\s+BG\s+(A|DE)\s+', '', d, flags=re.I)
    d = re.sub(r'^BANCA\s+(MOVIL|EN\s+LINEA)\s+TRANSFERENCIA\s+(A|DE)\s+(\d+\s+)?', '', d, flags=re.I)
    d = re.sub(r'\s+A\s+TERCEROS$', '', d, flags=re.I)
    d = re.sub(r'\s+POR\s+.*$', '', d)          # Yappy memo ("POR futbol-...")
    d = re.sub(r'\s+\(O\)\s+.*$', '', d)        # joint-holder suffix
    d = d.strip(' -,')
    return d.title() if d.isupper() else d


def table_rows(content: bytes) -> list:
    """Every data row from every page's table, as [fecha, desc, monto, saldo]."""
    out = []
    for page in pdf_pages(content):
        for table in page.extract_tables():
            for r in table:
                cells = [clean_desc(c or '') for c in r]
                if len(cells) != 4 or [c.lower() for c in cells] == HEADER:
                    continue
                out.append(cells)
    return out


def parse(content: bytes, filename: str) -> ParseResult:
    text = pdf_text(content)
    if 'Fecha Descripción Monto Saldo total' not in text:
        raise ValueError('Not a Banco General "Últimos movimientos" PDF')

    holder_m = HOLDER_RE.search(text)
    holder = holder_m.group(1).strip().title() if holder_m else None

    parsed = []     # (date, monto, saldo, desc), in file order (newest first)
    for fecha, desc, monto_s, saldo_s in table_rows(content):
        d = pdate(fecha)
        if d is None:
            raise ValueError(f'Unparseable Banco General row: {[fecha, desc, monto_s, saldo_s]}')
        parsed.append((d, money(monto_s), money(saldo_s), desc))

    if not parsed:
        raise ValueError('No movement rows found on Banco General PDF')

    # ---- reconcile: running-balance chain + printed Saldo total ------------
    problems = []
    for newer, older in zip(parsed, parsed[1:]):
        expected = round(older[2] + newer[1], 2)
        if abs(expected - newer[2]) > RECONCILE_TOLERANCE:
            problems.append(f'{newer[0]} "{newer[3]}": balance {newer[2]:.2f} != '
                            f'{older[2]:.2f} + {newer[1]:.2f}')
    closing = parsed[0][2]
    opening = round(parsed[-1][2] - parsed[-1][1], 2)
    saldo_m = SALDO_RE.search(text)
    if not saldo_m:
        problems.append('printed "Saldo total" not found in header')
    elif abs(money(saldo_m.group(1)) - closing) > RECONCILE_TOLERANCE:
        problems.append(f'printed Saldo total {saldo_m.group(1)} != newest row balance {closing:.2f}')

    ok = not problems
    if ok:
        detail = (f'{len(parsed)} rows; running balance chain OK from opening {opening:.2f} '
                  f'to closing {closing:.2f} (matches printed Saldo total).')
    else:
        detail = 'Banco General running-balance check failed: ' + '; '.join(problems[:5])

    txns = [
        TxnRow(date=d, amount=round(-monto, 2), type=txn_type(desc, monto), description=desc,
               merchant=clean_merchant(desc), card=ACCOUNT_NAME, cardholder=holder, balance=saldo)
        for (d, monto, saldo, desc) in reversed(parsed)     # oldest first
    ]
    start, end = txns[0].date, txns[-1].date

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=end,
        period_label=f'{start}..{end}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME], dedupe_against_ledger=True,
    )
