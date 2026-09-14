"""Capital One 360 Checking + Savings PDF bank statements.

A single PDF covers both sub-accounts (...9493 Checking and ...9130
Savings), so this parser returns rows tagged with whichever account each
row actually belongs to and reconciles each sub-account separately against
its own "opening -> closing balance" line from the account summary.

Ported from the household's reference script ``extract_bank.py``.
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import MON, money, ParseResult, TxnRow, pdf_text, RECONCILE_TOLERANCE

ROW_RE = re.compile(
    r'^([A-Z][a-z]{2}) (\d{1,2}) (.*?)\s*(Credit|Debit)\s*([+-])\s*\$([\d,]+\.\d{2})\s+\$([\d,]+\.\d{2})$')


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if '360 checking' in fn or '360 savings' in fn or 'capital one 360' in fn:
        return True
    try:
        text = pdf_text(content)
    except Exception:
        return False
    return '360 Checking' in text or '360 Performance Savings' in text


def classify_out(desc: str) -> str:
    u = desc.upper()
    if 'DEBIT CARD' in u or u.startswith('POS ') or 'PURCHASE' in u:
        return 'Purchase'
    return 'Payment'


def classify_in(desc: str) -> str:
    return 'Interest' if 'INTEREST' in desc.upper() else 'Credit'


def parse(content: bytes, filename: str) -> ParseResult:
    text = pdf_text(content)

    pm = re.search(r'([A-Z][a-z]{2}) \d{1,2} - ([A-Z][a-z]{2}) \d{1,2}, (\d{4})', text)
    if not pm:
        raise ValueError('Could not find statement period on Capital One 360 PDF')
    smon, emon, yr = MON[pm.group(1)], MON[pm.group(2)], int(pm.group(3))

    def yfor(mon):
        return yr - 1 if (smon == 12 and mon == 12 and emon != 12) else yr

    summ = {}
    for sm_m in re.finditer(
            r'360 (?:Checking|Performance Savings)\.\.\.(\d{4}) \$([\d,]+\.\d{2}) \$([\d,]+\.\d{2})', text):
        summ[sm_m.group(1)] = (money(sm_m.group(2)), money(sm_m.group(3)))

    lines = [l.rstrip() for l in text.splitlines()]
    acct = None
    pend_pre = ''
    rows = []       # (subcard, date, amount, type, desc, merchant, balance)
    i = 0
    while i < len(lines):
        ln = lines[i].strip()
        i += 1
        ma = re.match(r'^360 (Checking|Performance Savings) - \d+(\d{4})$', ln)
        if ma:
            acct = ma.group(2)
            pend_pre = ''
            continue
        if acct is None or acct not in accounts.CAPONE_360_BY_SUBCARD:
            continue
        if 'Opening Balance' in ln or 'Closing Balance' in ln or 'Interest Rate Change' in ln:
            pend_pre = ''
            continue
        m = ROW_RE.match(ln)
        if not m:
            if ln and not ln.startswith('Page ') and not ln.startswith('Trevor') \
               and 'STATEMENT PERIOD' not in ln and not ln.startswith('DATE ') \
               and 'ANNUAL PERCENTAGE' not in ln and not ln.startswith('capitalone') \
               and not re.match(r'^\d', ln) and len(ln) < 90:
                pend_pre = (pend_pre + ' ' + ln).strip()
            continue
        mon, day = MON[m.group(1)], int(m.group(2))
        inline = m.group(3).strip()
        cat = m.group(4)
        amt = money(m.group(6))
        bal = money(m.group(7))
        desc = (pend_pre + ' ' + inline).strip()
        pend_pre = ''
        if inline == '' and i < len(lines):
            nx = lines[i].strip()
            if nx and not ROW_RE.match(nx) and not re.match(r'^([A-Z][a-z]{2}) \d', nx) \
               and 'Balance' not in nx and not nx.startswith('360 ') \
               and 'ANNUAL PERCENTAGE' not in nx and not nx.startswith('Page ') \
               and len(nx) < 90 and not nx.startswith('Trevor'):
                desc = (desc + ' ' + nx).strip()
                i += 1
        signed = amt if cat == 'Debit' else -amt
        typ = classify_out(desc) if cat == 'Debit' else classify_in(desc)
        d = datetime.date(yfor(mon), mon, day)
        desc = re.sub(r'\s{2,}', ' ', desc).strip()
        merch = re.sub(r'\s+XXXXX+\w*', '', desc).strip()
        rows.append((acct, d, round(signed, 2), typ, desc, merch, round(bal, 2)))

    txns = [
        TxnRow(date=d, amount=amt, type=typ, description=desc, merchant=merch,
               card=accounts.CAPONE_360_BY_SUBCARD[subcard], balance=bal)
        for (subcard, d, amt, typ, desc, merch, bal) in rows
    ]

    detail_parts = []
    all_ok = True
    accounts_covered = set()
    for a in ('9493', '9130'):
        if a not in summ:
            continue
        accounts_covered.add(accounts.CAPONE_360_BY_SUBCARD[a])
        st, en = summ[a]
        signed_sum = sum(amt for (subcard, _d, amt, *_r) in rows if subcard == a)
        # opening - net_out(=amt positive) ... amt sign: positive=out, so
        # closing = opening - signed_sum (since signed_sum uses out=+/in=-)
        calc = round(st - signed_sum, 2)
        ok = abs(calc - en) < RECONCILE_TOLERANCE
        all_ok = all_ok and ok
        detail_parts.append(f'{accounts.CAPONE_360_BY_SUBCARD[a]}: {st:.2f} -> {en:.2f}, calc {calc:.2f} ({"OK" if ok else "MISMATCH"})')

    if not summ:
        all_ok = False
        detail_parts.append('Could not find account summary balances on statement')

    close_date = datetime.date(yr, emon, 1)
    if txns:
        close_date = max(t.date for t in txns)

    return ParseResult(
        account_name=None, rows=txns, statement_period=close_date,
        period_label=f'{yr}-{smon:02d}..{yr}-{emon:02d}',
        reconciliation_ok=all_ok, reconciliation_detail='; '.join(detail_parts),
        accounts_covered=sorted(accounts_covered),
    )
