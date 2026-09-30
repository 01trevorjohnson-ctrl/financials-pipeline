"""BAC Panama debit account (...0794) "Consulta de Transferencias" CSV
export: outgoing transfers only (to other BAC clients and ACH to other
banks), with the recipient's name -- which the account statement itself
doesn't always show (a BAC-to-BAC transfer there is just "TEF A : <acct>").

Layout: a small header block ("Producto","115280794" / "Fecha Desde" /
"Fecha Hasta" ...) then a table
``Estado, Fecha (DD/MM/YYYY), Referencia, Cuenta Origen, Cuenta Destino
("<acct> - <name>"), Monto, Moneda``. Only "Enviada" (sent) rows are money
that actually left the account; anything else is skipped and counted.

NOTE ON RECONCILIATION: like the Robinhood spending CSV, this listing
carries no balance or total, so there is nothing independent to reconcile
against; we do a structural check (every sent row parses to a valid date,
amount and USD currency) and say so in ``reconciliation_detail``.

OVERLAP: these transfers also appear on the account's monthly statement
(same dates, per the Sep 2026 export vs. the ledger), so whichever of the
two is loaded second only adds what's new -- ``main.py`` skips rows already
in the ledger by (date, amount).
"""
from __future__ import annotations

import datetime
import re

from .. import accounts
from .common import ParseResult, TxnRow, clean_desc, csv_rows, num
from .panama_debit_bac import HOLDER

ACCOUNT_NAME = accounts.PANAMA_DEBIT
ACCOUNT_NUMBER = '115280794'
TITLE = 'Consulta de Transferencias'
DATE_RE = re.compile(r'^(\d{2})/(\d{2})/(\d{4})$')


def _rows(content: bytes):
    return csv_rows(content, encoding_candidates=('utf-8-sig', 'cp1252', 'latin-1'))


def matches(filename: str, content: bytes) -> bool:
    try:
        rows = _rows(content)
    except Exception:
        return False
    head = [[c.strip() for c in r] for r in rows[:15]]
    return (any(r and r[0] == TITLE for r in head)
            and any(len(r) > 1 and r[0] == 'Producto' and r[1] == ACCOUNT_NUMBER for r in head))


def pdate(s: str):
    m = DATE_RE.match((s or '').strip())
    if not m:
        return None
    return datetime.date(int(m.group(3)), int(m.group(2)), int(m.group(1)))


def parse(content: bytes, filename: str) -> ParseResult:
    data = [[c.strip() for c in r] for r in _rows(content)]
    if not matches(filename, content):
        raise ValueError(f'Not a BAC "{TITLE}" export for account {ACCOUNT_NUMBER}')

    header_i = next((i for i, r in enumerate(data) if r[:2] == ['Estado', 'Fecha']), None)
    if header_i is None:
        raise ValueError(f'No "Estado","Fecha",... table header in BAC "{TITLE}" export')
    cols = {name: j for j, name in enumerate(data[header_i]) if name}
    need = ['Estado', 'Fecha', 'Referencia', 'Cuenta Destino', 'Monto', 'Moneda']
    missing = [c for c in need if c not in cols]
    if missing:
        raise ValueError(f'BAC "{TITLE}" export is missing column(s): {", ".join(missing)}')

    txns, skipped, problems = [], {}, []
    for r in data[header_i + 1:]:
        if not any(r):
            continue
        get = lambda c: r[cols[c]] if cols[c] < len(r) else ''  # noqa: E731
        status = get('Estado')
        if status != 'Enviada':
            skipped[status or '(blank)'] = skipped.get(status or '(blank)', 0) + 1
            continue
        d, amount, currency = pdate(get('Fecha')), get('Monto'), get('Moneda')
        try:
            amt = round(num(amount), 2)
        except ValueError:
            amt = None
        if d is None or amt is None or amt <= 0 or currency != 'USD':
            problems.append(f'{get("Fecha")} {get("Cuenta Destino")!r}: {amount} {currency}')
            continue
        dest = get('Cuenta Destino')
        acct, _, name = dest.partition(' - ')
        name = clean_desc(name) or clean_desc(acct)
        txns.append(TxnRow(
            date=d, amount=amt, type='Purchase',
            description=f'TRANSFERENCIA A {name} ({acct.strip()}) ref {get("Referencia")}',
            merchant=name, card=ACCOUNT_NAME, cardholder=HOLDER))

    if not txns and not problems:
        raise ValueError(f'No sent ("Enviada") transfers in BAC "{TITLE}" export')

    ok = not problems
    skipped_note = ('; skipped not-sent rows: ' + ', '.join(f'{n} {s}' for s, n in skipped.items())
                    if skipped else '')
    detail = (f'{len(txns)} sent transfer(s) parsed{skipped_note}. This transfers listing has no '
              'balance or total to reconcile against; structural parse check only.' if ok
              else 'Unparseable BAC transfer row(s): ' + '; '.join(problems[:5]))
    txns.sort(key=lambda t: t.date)
    start, end = (txns[0].date, txns[-1].date) if txns else (None, None)
    return ParseResult(
        account_name=ACCOUNT_NAME, rows=txns, statement_period=end,
        period_label=f'{start}..{end}',
        reconciliation_ok=ok, reconciliation_detail=detail,
        accounts_covered=[ACCOUNT_NAME],
    )
