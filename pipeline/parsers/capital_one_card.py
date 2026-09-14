"""Capital One Quicksilver credit-card PDF statements.

Ported from the household's reference script ``extract.py`` (function
``parse_capone`` + ``clean_merchant``), adapted to work on in-memory PDF
bytes instead of a filepath, and to reconcile a single file rather than a
whole-folder batch.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import MON, AMT_RE, money, ParseResult, TxnRow, pdf_text, RECONCILE_TOLERANCE

ACCOUNT_NAME = accounts.CAPONE_QUICKSILVER

LINE_RE = re.compile(
    r'^([A-Z][a-z]{2}) (\d{1,2}) ([A-Z][a-z]{2}) (\d{1,2}) (.+?) (-? ?\$[\d,]+\.\d{2})$')


def matches(filename: str, content: bytes) -> bool:
    if 'quicksilver' in filename.lower():
        return True
    try:
        return 'Quicksilver' in pdf_text(content)
    except Exception:
        return False


def clean_merchant(desc: str) -> str:
    d = re.sub(r'\s+', ' ', desc.strip())
    if ' ' not in d:
        # older Capital One rows glue CITY+ST onto the name: ...RICHFIELDMN
        d = re.sub(r'(PANAMPAN|PANAMAPAN|[A-Z]{2})$', '', d)
        d = re.sub(r'\d{5,}', ' ', d)
        return d.strip(' -')
    d = re.sub(r'\s+[A-Za-z0-9.\'&/-]+(?:\s+[A-Za-z0-9.\'&/-]+){0,2}\s+[A-Z]{2}$', '', d)
    d = re.sub(r'\s+(PANAMA|PANAM)\s+(PAN|PB)$', '', d)
    d = re.sub(r'\s+\d{3,}$', '', d)
    return d.strip(' -')


def parse(content: bytes, filename: str) -> ParseResult:
    full = pdf_text(content)
    lines = [l.rstrip() for l in full.splitlines()]

    m = re.search(r'([A-Z][a-z]{2}) (\d{1,2}), (\d{4}) - ([A-Z][a-z]{2}) (\d{1,2}), (\d{4})', full)
    if not m:
        raise ValueError('Could not find statement period on Capital One Quicksilver PDF')
    sm, sy, em, ey = MON[m.group(1)], int(m.group(3)), MON[m.group(4)], int(m.group(6))
    close_date = datetime.date(ey, em, int(m.group(5)))

    def year_for(mon):
        if sy == ey:
            return sy
        return sy if mon == 12 else ey

    rows = []
    section = None
    stated = {}
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if s.endswith(': Payments, Credits and Adjustments'):
            section = 'PAY'; continue
        if s.endswith(': Transactions'):
            section = 'PUR'; continue
        if s == 'Fees':
            section = 'FEE'; continue
        if s.startswith('Interest Charged'):
            section = 'INT'; continue
        if s.startswith('Transactions (Continued)'):
            continue
        mt = re.search(r'Total Transactions for This Period (\$[\d,.]+)', s)
        if mt:
            stated['transactions'] = money(mt.group(1))
        mt = re.search(r'Total Fees for This Period (\$[\d,.]+)', s)
        if mt:
            stated['fees'] = money(mt.group(1))
        mt = re.search(r'Total Interest for This Period (\$[\d,.]+)', s)
        if mt:
            stated['interest'] = money(mt.group(1))

        if section == 'INT':
            mi = re.match(r'^(Interest Charge on .+?) (\$[\d,]+\.\d{2})$', s)
            if mi and money(mi.group(2)) != 0:
                rows.append((close_date, money(mi.group(2)), 'Interest', mi.group(1), mi.group(1), 'INT'))
            continue

        if section not in ('PAY', 'PUR', 'FEE'):
            continue
        m2 = LINE_RE.match(s)
        if not m2:
            continue
        tmon, tday = MON[m2.group(1)], int(m2.group(2))
        d = datetime.date(year_for(tmon), tmon, tday)
        desc = m2.group(5).strip()
        amt = money(m2.group(6).replace(' ', ''))
        if section == 'PAY':
            up = desc.upper()
            typ = 'Payment' if ('PYMT' in up or 'PAYMENT' in up) else 'Credit'
        elif section == 'FEE':
            typ = 'Fee'
        else:
            typ = 'Purchase'
        rows.append((d, amt, typ, desc, clean_merchant(desc), section))

    txns = [
        TxnRow(date=d, amount=round(amt, 2), type=typ, description=desc,
               merchant=merch, card=ACCOUNT_NAME)
        for (d, amt, typ, desc, merch, _sec) in rows
    ]

    pur = sum(amt for (_d, amt, typ, _de, _me, sec) in rows if sec == 'PUR')
    fee = sum(amt for (_d, amt, typ, _de, _me, sec) in rows if sec == 'FEE')
    target = stated.get('transactions')
    if target is None:
        ok, detail = False, 'Could not find "Total Transactions for This Period" on statement'
    else:
        diff = min(abs(pur - target), abs(pur + fee - target))
        ok = diff < RECONCILE_TOLERANCE
        detail = f'parsed purchases {pur:.2f} (+fees {fee:.2f}) vs statement total {target:.2f}, diff {diff:.2f}'

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=f'{sy}-{sm:02d}..{ey}-{em:02d}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
