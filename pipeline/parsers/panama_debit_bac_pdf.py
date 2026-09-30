"""Panama debit account (...0794) PDF statement ("RESUMEN DE CUENTA
BANCARIA" / "DETALLE DE CUENTA", account 115280794), BAC Credomatic --
the PDF counterpart of ``panama_debit_bac.py``'s CSV export.

Detail rows: ``MON/DD  reference  concepto  amount  saldo``. There is one
amount column in the extracted text (Débito and Crédito are side by side
in the layout), so the direction comes from the running Saldo: money out
when the balance drops. Dates carry no year; it comes from the statement
date in the header (``ENE/31/26``), stepping back a year for rows in a
later month than the statement (a December row on a January statement).

Reconciliation: every row's amount must equal the change in the printed
running balance, starting from "Saldo Anterior", and the last balance must
equal the "Saldo al Corte" figure.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .panama_debit_bac import HOLDER, clean_merchant
from .common import MON_ES, RECONCILE_TOLERANCE, ParseResult, TxnRow, clean_desc, num, pdf_text

ACCOUNT_NAME = accounts.PANAMA_DEBIT
ACCOUNT_NUMBER = '115280794'
STMT_DATE_RE = re.compile(r'\b([A-Z]{3})/(\d{2})/(\d{2})\b')
ROW_RE = re.compile(r'^([A-Z]{3})/(\d{2})\s+(\d{6,})\s+(.+?)\s+([\d,]+\.\d{2})\s+(-?[\d,]+\.\d{2})$')
PREV_RE = re.compile(r'^Saldo Anterior\s+(-?[\d,]+\.\d{2})$', re.M)
CLOSE_RE = re.compile(r'^Saldo al Corte\s+.*?(-?[\d,]+\.\d{2})$', re.M)
FEE_PREFIXES = ('PROTECCION ROBO', 'VALOR DE TARJETA', 'COMISION', 'CARGO')


def matches(filename: str, content: bytes) -> bool:
    try:
        text = pdf_text(content)
    except Exception:
        return False
    return 'RESUMEN DE CUENTA BANCARIA' in text and ACCOUNT_NUMBER in text


def txn_type(desc: str, amount: float) -> str:
    up = desc.upper()
    if amount < 0:
        return 'Interest' if 'INTERES' in up else 'Credit'
    if up.startswith(FEE_PREFIXES):
        return 'Fee'
    if re.match(r'^PAGO \d{6}\*+\d{4}$', up):     # paying one of the household's cards
        return 'Payment'
    return 'Purchase'


def parse(content: bytes, filename: str) -> ParseResult:
    text = pdf_text(content)
    if 'DETALLE DE CUENTA' not in text or ACCOUNT_NUMBER not in text:
        raise ValueError('Not a BAC Panama debit account (...0794) PDF statement')

    sd = STMT_DATE_RE.search(text)
    if not sd or sd.group(1).lower() not in MON_ES:
        raise ValueError('Could not find the statement date (e.g. "ENE/31/26") on BAC debit PDF')
    stmt_month, stmt_year = MON_ES[sd.group(1).lower()], 2000 + int(sd.group(3))

    prev_m, close_m = PREV_RE.search(text), CLOSE_RE.search(text)
    if not prev_m or not close_m:
        raise ValueError('Could not find "Saldo Anterior" and/or "Saldo al Corte" on BAC debit PDF')
    opening, closing = num(prev_m.group(1)), num(close_m.group(1))

    rows = []           # (date, signed amount, desc, printed amount, balance)
    problems = []
    bal = opening
    for ln in text.splitlines():
        m = ROW_RE.match(ln.strip())
        if not m or m.group(1).lower() not in MON_ES:
            continue
        mon, dd = MON_ES[m.group(1).lower()], int(m.group(2))
        year = stmt_year - 1 if mon > stmt_month else stmt_year
        desc = clean_desc(m.group(4))
        printed, new_bal = num(m.group(5)), num(m.group(6))
        signed = round(bal - new_bal, 2)            # money out = positive
        if abs(abs(signed) - printed) > RECONCILE_TOLERANCE:
            problems.append(f'{year}-{mon:02d}-{dd:02d} "{desc}": amount {printed:.2f} but balance '
                            f'moved {bal:.2f} -> {new_bal:.2f}')
        rows.append((datetime.date(year, mon, dd), signed, desc, new_bal))
        bal = new_bal

    if not rows:
        raise ValueError('No transaction rows found on BAC debit PDF')
    if abs(bal - closing) > RECONCILE_TOLERANCE:
        problems.append(f'last running balance {bal:.2f} != Saldo al Corte {closing:.2f}')

    ok = not problems
    detail = (f'{len(rows)} rows; running balance OK from Saldo Anterior {opening:.2f} to '
              f'Saldo al Corte {closing:.2f}.' if ok
              else 'BAC debit PDF running-balance check failed: ' + '; '.join(problems[:5]))

    txns = [
        TxnRow(date=d, amount=amt, type=txn_type(desc, amt), description=desc,
               merchant=clean_merchant(desc), card=ACCOUNT_NAME, cardholder=HOLDER, balance=b)
        for (d, amt, desc, b) in rows
    ]
    start, end = min(t.date for t in txns), max(t.date for t in txns)
    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=end,
        period_label=f'{start}..{end}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
