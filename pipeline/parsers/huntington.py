"""Huntington Checking (...3491) PDF bank statement.

Ported from the household's reference script ``extract_bank.py``.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import money, ParseResult, TxnRow, pdf_text, RECONCILE_TOLERANCE

ACCOUNT_NAME = accounts.HUNTINGTON_CHECKING


def matches(filename: str, content: bytes) -> bool:
    if 'huntington' in filename.lower():
        return True
    try:
        return 'HUNTINGTON' in pdf_text(content).upper()
    except Exception:
        return False


def classify_out(desc: str) -> str:
    u = desc.upper()
    if 'DEBIT CARD' in u or u.startswith('POS ') or 'PURCHASE' in u:
        return 'Purchase'
    return 'Payment'


def classify_in(desc: str) -> str:
    return 'Interest' if 'INTEREST' in desc.upper() else 'Credit'


def parse(content: bytes, filename: str) -> ParseResult:
    text = pdf_text(content)
    if 'HUNTINGTON' not in text.upper():
        raise ValueError('Not a Huntington statement')

    pm = re.search(r'StatementPeriodfrom(\d{2})/(\d{2})/(\d{2})to(\d{2})/(\d{2})/(\d{2})',
                    text.replace(' ', ''))
    if not pm:
        raise ValueError('Could not find statement period on Huntington PDF')
    sm, sy = int(pm.group(1)), 2000 + int(pm.group(3))
    em, ey = int(pm.group(4)), 2000 + int(pm.group(6))

    def yfor(mon):
        if sy == ey:
            return sy
        return sy if mon >= sm else ey

    bb = re.search(r'Beginning Balance \$([\d,]+\.\d{2})', text)
    end_m = re.search(r'Ending Balance \$([\d,]+\.\d{2})', text)

    lines = text.splitlines()
    section = None
    rows = []
    sum_c = sum_d = 0.0
    j = 0
    n = len(lines)
    while j < n:
        ln = lines[j].strip()
        j += 1
        if 'Deposit / Credit Activity' in ln:
            section = 'C'; continue
        if 'Other Withdrawal / Debit Activity' in ln or 'Other Debit' in ln \
           or 'Electronic Withdrawal' in ln or 'Card Withdrawal' in ln:
            section = 'D'; continue
        if 'Balance Activity' in ln or ln.startswith('Investments') or ln.startswith('In the Event') \
           or ln.startswith('Date Description Amount'):
            if 'Balance Activity' in ln or ln.startswith('Investments') or ln.startswith('In the Event'):
                section = None
            continue
        if section not in ('C', 'D'):
            continue

        def emit(mon, day, desc, amt):
            nonlocal sum_c, sum_d
            desc = re.sub(r'\s{2,}', ' ', desc).strip()
            signed = -amt if section == 'C' else amt
            typ = classify_in(desc) if section == 'C' else classify_out(desc)
            d = datetime.date(yfor(mon), mon, day)
            merch = re.sub(r'\d{6,}.*$', '', desc).strip(' -')
            rows.append((d, round(signed, 2), typ, desc, merch))
            if section == 'C':
                sum_c += amt
            else:
                sum_d += amt

        m = re.match(r'^(\d{2})/(\d{2}) (.+?) ([\d,]+\.\d{2})$', ln)
        if m:
            emit(int(m.group(1)), int(m.group(2)), m.group(3), money(m.group(4)))
            continue
        md = re.match(r'^(\d{2})/(\d{2}) (.+)$', ln)
        if md and j < n:
            nx = lines[j].strip()
            ma = re.match(r'^(.*?)\s*([\d,]+\.\d{2})$', nx)
            if ma and not re.match(r'^\d{2}/\d{2} ', nx):
                j += 1
                desc = (md.group(3).strip() + ' ' + ma.group(1).strip()).strip()
                emit(int(md.group(1)), int(md.group(2)), desc, money(ma.group(2)))

    txns = [
        TxnRow(date=d, amount=amt, type=typ, description=desc, merchant=merch, card=ACCOUNT_NAME)
        for (d, amt, typ, desc, merch) in rows
    ]

    if bb and end_m:
        beg = money(bb.group(1))
        end = money(end_m.group(1))
        calc = beg + sum_c - sum_d
        ok = abs(calc - end) < RECONCILE_TOLERANCE
        detail = f'beg {beg:.2f} + credits {sum_c:.2f} - debits {sum_d:.2f} = {calc:.2f} vs ending balance {end:.2f}'
    else:
        ok, detail = False, 'Could not find Beginning/Ending Balance on statement'

    close_date = datetime.date(ey, em, 1)
    if txns:
        close_date = max(t.date for t in txns)

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=f'{sy}-{sm:02d}..{ey}-{em:02d}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
