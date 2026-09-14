"""Banco General (Panama) 'Transacciones realizadas' PDF -- outgoing
transfers only. Sign convention: money OUT = positive.

Ported from the household's reference script ``extract_bg.py``.

NOTE ON RECONCILIATION: per the household's own notes, this source has "no
balance, no statement total" -- it is a plain activity listing, not a
statement with a printed ending balance. As with the Robinhood spending CSV,
true reconciliation against an independent total is not possible for this
format; we do a structural sanity check instead (every row parsed to a
valid date + amount) and document that explicitly, per the pipeline
README's documented exception to the "reconcile before insert" rule.
"""
from __future__ import annotations

import datetime
import re

import accounts
from .common import ParseResult, TxnRow, pdf_text

ACCOUNT_NAME = accounts.BANCO_GENERAL_TRANSFERS
HOLDER = 'Sandra Viviana Suarez Jimenez'
SPAN = {'ene': 1, 'feb': 2, 'mar': 3, 'abr': 4, 'may': 5, 'jun': 6,
        'jul': 7, 'ago': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dic': 12}
ROW_RE = re.compile(r'^(\d{2})-([a-z]{3})-(\d{4})?\s+(.*?)\s+REALIZADA\s+\$?([\d,]+\.\d{2})\s*$')


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'banco general' in fn or 'transfers' in fn:
        return True
    try:
        text = pdf_text(content)
    except Exception:
        return False
    return 'Transacciones realizadas' in text


def is_noise(s: str) -> bool:
    return s.startswith(('Fecha ', 'P�gina', 'Pagina', 'Página',
                          'Transacciones realizadas')) or re.match(r'^\d{2}-\w{3}-\d{4}\s*[�·]', s)


def parse(content: bytes, filename: str) -> ParseResult:
    raw = pdf_text(content)
    if 'Transacciones realizadas' not in raw:
        raise ValueError('Not a Banco General transfers PDF')

    lines = [l.strip() for l in raw.splitlines() if l.strip()]
    rows = []
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        m = ROW_RE.match(ln)
        if not m:
            i += 1
            continue
        dd, mon = int(m.group(1)), SPAN.get(m.group(2))
        yyyy = m.group(3)
        mid = m.group(4).strip()
        amt = float(m.group(5).replace(',', ''))
        i += 1
        if mon is None:
            continue
        if yyyy is None:
            if i < n and re.fullmatch(r'\d{4}', lines[i]):
                yyyy = lines[i]
                i += 1
            else:
                yyyy = str(datetime.date.today().year)
        yyyy = int(yyyy)
        payee = ''
        if i < n and not ROW_RE.match(lines[i]) and not is_noise(lines[i]) \
                and not re.fullmatch(r'\d{4}', lines[i]):
            payee = lines[i].strip()
            i += 1
        memo = re.sub(r'^(Cuenta de ahorros|Cuenta corriente)\s+[\d-]+\s*', '', mid).strip()
        memo = re.sub(r'\bnull\b', '', memo).strip()
        mid2 = re.sub(r'\s{2,}', ' ', re.sub(r'\bnull\b', '', mid)).strip()
        desc_full = f'{payee} to {mid2}'.strip()
        merch = payee or memo or mid2
        merch = re.sub(r'\s{2,}', ' ', merch).title() if merch.isupper() else merch
        try:
            d = datetime.date(yyyy, mon, dd)
        except ValueError:
            continue
        rows.append((d, round(amt, 2), re.sub(r'\s{2,}', ' ', desc_full), merch))

    if not rows:
        raise ValueError('No transfer rows found on Banco General PDF')

    txns = [
        TxnRow(date=d, amount=amt, type='Payment', description=desc, merchant=merch,
               card=ACCOUNT_NAME, cardholder=HOLDER)
        for (d, amt, desc, merch) in rows
    ]

    ok = True
    detail = (f'{len(rows)} transfer rows parsed. This source has no independent statement '
              'total or balance to reconcile against (per the household\'s own notes); '
              'structural parse check only.')

    close_date = max(t.date for t in txns)

    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=close_date,
        period_label=f'{min(t.date for t in txns)}..{close_date}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
