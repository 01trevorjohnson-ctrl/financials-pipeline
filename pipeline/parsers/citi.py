"""Citi AAdvantage Platinum + Citi Costco Anywhere Visa PDF statements.

Ported from the household's reference script ``extract.py`` (``parse_citi``,
``citi_page_text``, ``_emit``, ``clean_merchant``, ``identify``). The
two-column jumble workaround (``citi_page_text``) is the trickiest bit: on
some statement layouts pdfplumber's default reading order interleaves a
right-hand rewards/legal column into the transaction lines, so we detect
that and crop the page to its left column before re-extracting text.
"""
from __future__ import annotations

import datetime
import os
import re

import accounts
from .common import MON, AMT_RE, money, ParseResult, TxnRow, pdf_pages, RECONCILE_TOLERANCE

SEC_HEADS = {
    'Payments, Credits and Adjustments': 'PAY',
    'Standard Purchases': 'PUR',
    "Standard Purchases, cont'd": 'PUR',
    'Promo Purchase-Offer': 'PUR',
    'Promo Purchases': 'PUR',
    'Fees Charged': 'FEE',
    'Interest Charged': 'INT',
    'Billing Disputes': 'DISPUTE',
}
CITI_STOP = ('2026 totals year-to-date', 'Interest charge calculation', 'Account messages',
             'CARDHOLDER SUMMARY')


def matches(filename: str, content: bytes) -> bool:
    fn = filename.lower()
    if 'aadvantage' in fn or 'costco' in fn:
        return True
    try:
        full = "\n".join(p.extract_text() or '' for p in pdf_pages(content))
    except Exception:
        return False
    return 'Costco Anywhere' in full or 'AADVANTAGE' in full.upper()


def identify(content: bytes):
    full = "\n".join(p.extract_text() or '' for p in pdf_pages(content))
    if 'Costco Anywhere' in full:
        return accounts.CITI_COSTCO, '146600'
    if 'AADVANTAGE' in full.upper():
        return accounts.CITI_AADVANTAGE, '513000'
    raise ValueError('Could not identify Citi card (not AAdvantage or Costco)')


def citi_page_text(page) -> str:
    full = page.extract_text() or ''
    jumbled = False
    for ln in full.splitlines():
        m = re.search(r'\$[\d,]+\.\d{2}', ln)
        if m and re.search(r'[A-Za-z]{4,}', ln[m.end():]):
            jumbled = True
            break
        if re.match(r'^[A-Za-z].{0,30}\s+\d{2}/\d{2}\s+\d{2}/\d{2}\s+\$', ln):
            jumbled = True
            break
    if jumbled:
        return page.crop((0, 0, 402, page.height)).extract_text() or ''
    return full


def clean_merchant(desc: str) -> str:
    d = re.sub(r'\s+', ' ', desc.strip())
    d = re.sub(r'\s+[A-Za-z0-9.\'&/-]+(?:\s+[A-Za-z0-9.\'&/-]+){0,2}\s+[A-Z]{2}$', '', d)
    d = re.sub(r'\s+(PANAMA|PANAM)\s+(PAN|PB)$', '', d)
    d = re.sub(r'\s+\d{3,}$', '', d)
    return d.strip(' -')


def _emit(rows, d, amt, desc, section):
    up = desc.upper()
    if section == 'PAY':
        if 'AUTOPAY' in up or 'PAYMENT' in up or 'AUTO-PMT' in up:
            typ = 'Payment'
        elif amt > 0:
            typ = 'Adjustment'
        else:
            typ = 'Credit'
    elif section == 'FEE':
        typ = 'Fee'
    elif section == 'INT':
        typ = 'Interest'
        if amt == 0:
            return
    else:
        typ = 'Adjustment' if 'MOVED TO STANDARD PURCH' in up else 'Purchase'
    rows.append((d, amt, typ, desc, clean_merchant(desc), section))


def parse(content: bytes, filename: str) -> ParseResult:
    card, subcard_digits = identify(content)
    pages = pdf_pages(content)
    pages_text = [p.extract_text() or '' for p in pages]
    full = "\n".join(pages_text)
    text = "\n".join(citi_page_text(p) for p in pages)

    pm = re.search(r'(\d{2})/(\d{2})/(\d{2})\s*-\s*(\d{2})/(\d{2})/(\d{2})', full)
    if pm:
        sm, sy = int(pm.group(1)), 2000 + int(pm.group(3))
        em, ey = int(pm.group(4)), 2000 + int(pm.group(6))
    else:
        fnm = re.search(r'statement_([A-Za-z]{3})', filename)
        if not fnm:
            raise ValueError('Could not determine Citi statement period from PDF or filename')
        fmon = MON[fnm.group(1)]
        em, ey = fmon, datetime.date.today().year
        sm, sy = (fmon - 1) or 12, (ey if fmon > 1 else ey - 1)

    def year_for(mon):
        if sy == ey:
            return sy
        return sy if mon >= sm else ey

    stated = {}
    mt = re.search(r'New Charges\s*\$?([\d,]+\.\d{2})', full)
    if mt:
        stated['new_charges'] = float(mt.group(1).replace(',', ''))
    mt = re.search(r'Purchases\s*\+?\$([\d,]+\.\d{2})', full)
    if mt:
        stated['acct_purchases'] = float(mt.group(1).replace(',', ''))

    lines = [re.sub(r'\s{2,}', ' ', l.strip()) for l in text.splitlines()]
    lines = [re.sub(r'\b(' + subcard_digits + r')\b', '', l).strip() for l in lines]

    tx_re = re.compile(r'^(\d{2})/(\d{2})(?:\s+\d{2}/\d{2})?\s+(.*?)\s*(' + AMT_RE + r')$')
    tx_noamt_re = re.compile(r'^(\d{2})/(\d{2})(?:\s+\d{2}/\d{2})?\s*(.*)$')
    only_amt_re = re.compile(r'^(' + AMT_RE + r')$')

    rows = []
    section = None
    pre_frag = ''
    i = 0
    n = len(lines)
    while i < n:
        t = lines[i]
        i += 1
        if not t:
            continue
        hit = None
        for h, code in SEC_HEADS.items():
            if t == h or t.startswith(h + ' '):
                hit = code
        if hit:
            section = hit
            pre_frag = ''
            continue
        if any(t.startswith(s) for s in CITI_STOP):
            section = None
            pre_frag = ''
            continue
        if re.match(r'^TREVOR\s+JOHNSON$', t, re.I) or t.startswith('Sale') \
           or t.startswith('Date Date') or t.startswith('Description') \
           or t.startswith('ACCOUNT SUMMARY') or t.startswith('CARDHOLDER SUMMARY') \
           or t.startswith('www.citicards') or t.startswith('New Charges') \
           or t.startswith('Page ') or 'Customer Service' in t:
            continue
        if re.match(r'^(NAME:|DEPART:|[A-Z]{3} TO [A-Z]{3}\s*:)', t) or \
           ('CLASS:' in t and 'STOP:' in t):
            continue
        if section in (None, 'DISPUTE'):
            continue

        m = tx_re.match(t)
        if m:
            mon, day = int(m.group(1)), int(m.group(2))
            desc_inline = m.group(3).strip()
            amt = money(m.group(4))
            desc = (pre_frag + ' ' + desc_inline).strip()
            pre_frag = ''
            if not desc_inline:
                if i < n and lines[i] and not tx_noamt_re.match(lines[i]) \
                   and not any(lines[i].startswith(h) for h in SEC_HEADS):
                    desc = (desc + ' ' + lines[i]).strip()
                    i += 1
            try:
                d = datetime.date(year_for(mon), mon, day)
            except ValueError:
                continue
            _emit(rows, d, amt, re.sub(r'\s{2,}', ' ', desc).strip(), section)
            continue

        m = tx_noamt_re.match(t)
        if m and not only_amt_re.match(t):
            mon, day = int(m.group(1)), int(m.group(2))
            desc = (pre_frag + ' ' + m.group(3).strip()).strip()
            pre_frag = ''
            amt = None
            j = i
            while j < n and j < i + 3:
                nx = lines[j]
                if not nx:
                    j += 1
                    continue
                if any(nx.startswith(h) for h in SEC_HEADS) or tx_noamt_re.match(nx):
                    break
                ma = re.search(r'(' + AMT_RE + r')\s*$', nx)
                if ma:
                    extra = nx[:ma.start()].strip()
                    if extra:
                        desc = (desc + ' ' + extra).strip()
                    amt = money(ma.group(1))
                    i = j + 1
                    break
                else:
                    desc = (desc + ' ' + nx).strip()
                    j += 1
            if amt is None:
                continue
            try:
                d = datetime.date(year_for(mon), mon, day)
            except ValueError:
                continue
            _emit(rows, d, amt, re.sub(r'\s{2,}', ' ', desc).strip(), section)
            continue

        if only_amt_re.match(t):
            continue
        if section in ('PUR', 'PAY', 'FEE') and re.search(r'[A-Za-z]', t) \
           and 'TOTAL' not in t.upper():
            pre_frag = (pre_frag + ' ' + t).strip() if pre_frag else t

    txns = [
        TxnRow(date=d, amount=round(amt, 2), type=typ, description=desc,
               merchant=merch, card=card)
        for (d, amt, typ, desc, merch, _sec) in rows
    ]

    pur = sum(amt for (_d, amt, typ, _de, _me, sec) in rows if sec == 'PUR' and typ != 'Adjustment')
    adj = sum(amt for (_d, amt, typ, _de, _me, sec) in rows if typ == 'Adjustment')
    fee = sum(amt for (_d, amt, typ, _de, _me, sec) in rows if sec == 'FEE')
    target = stated.get('acct_purchases', stated.get('new_charges'))
    if target is None:
        ok, detail = False, 'Could not find "Purchases" / "New Charges" total on statement'
    else:
        diff = min(abs(pur - target), abs(pur + fee - target),
                   abs(pur + adj - target), abs(pur + fee + adj - target))
        ok = diff < RECONCILE_TOLERANCE
        detail = f'parsed purchases {pur:.2f} (+fees {fee:.2f} +adj {adj:.2f}) vs statement total {target:.2f}, diff {diff:.2f}'

    close_date = datetime.date(ey, em, 1)
    # Use the last day of the closing month as a reasonable statement_period
    # anchor if an exact close date isn't printed; prefer the max txn date.
    if txns:
        close_date = max(t.date for t in txns)

    return ParseResult(
        account_name=card, rows=txns, statement_period=close_date,
        period_label=f'{sy}-{sm:02d}..{ey}-{em:02d}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[card],
    )
