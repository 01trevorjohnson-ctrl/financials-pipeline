"""AI extraction fallback for statement files no dedicated parser matches.

Sends the file (PDF, CSV/text, or XLSX) to Claude with a strict JSON
schema and asks for what is *printed* on it: the account's last 4 digits,
each transaction (date, description, amount, direction, and the card it is
listed under), and the opening/closing balances. The pipeline then does its
own checks before anything is booked:

- **Account:** every row must resolve to one of the household's known
  accounts via ``accounts.ACCOUNT_BY_LAST4`` (a row's own card, else the
  statement's account). Anything unresolvable fails the file.
- **Reconciliation:** opening and closing balances are required, and the
  extracted rows must bridge them within ``RECONCILE_TOLERANCE`` -- for a
  deposit account ``opening - out + in = closing``, for a credit card
  (``accounts.CARD_ACCOUNTS``, balance = amount owed)
  ``opening + out - in = closing``. A statement with no printed balances
  can't be verified, so it fails rather than being guessed at.

A failure raises :class:`ExtractionError`; ``parse_file`` turns that into
the usual "unrecognized format" needs_review item, with the reason. Rows
that do pass are marked "AI-extracted" in ``reconciliation_detail``, so a
format that keeps arriving can be given a dedicated parser.
"""
from __future__ import annotations

import base64
import datetime
import io
import json
import logging
import os

from .. import accounts
from .common import RECONCILE_TOLERANCE, ParseResult, TxnRow, clean_desc, is_pdf, sniff_text

logger = logging.getLogger(__name__)

MODEL = 'claude-opus-5-5'
MAX_TOKENS = 32000
MAX_PDF_BYTES = 30 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
# Written into processed_statements.reconciliation_detail when extraction
# fails; main.py looks for it to avoid re-sending the same file every run.
AI_FAILED_MARKER = 'AI extraction failed'

SCHEMA = {
    'type': 'object',
    'properties': {
        'is_account_statement': {'type': 'boolean'},
        'institution': {'type': 'string'},
        'account_last4': {'type': ['string', 'null']},
        'currency': {'type': 'string'},
        'statement_end_date': {'type': ['string', 'null']},
        'opening_balance': {'type': ['number', 'null']},
        'closing_balance': {'type': ['number', 'null']},
        'transactions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'date': {'type': 'string'},
                    'description': {'type': 'string'},
                    'amount': {'type': 'number'},
                    'direction': {'type': 'string', 'enum': ['out', 'in']},
                    'card_last4': {'type': ['string', 'null']},
                },
                'required': ['date', 'description', 'amount', 'direction', 'card_last4'],
                'additionalProperties': False,
            },
        },
    },
    'required': ['is_account_statement', 'institution', 'account_last4', 'currency',
                 'statement_end_date', 'opening_balance', 'closing_balance', 'transactions'],
    'additionalProperties': False,
}

SYSTEM = """You transcribe bank and credit-card statements and account-activity exports \
into JSON, exactly as printed. A separate program checks your numbers against the printed \
balances, so accuracy matters more than completeness of anything else: never estimate, \
infer, or invent a value.

- is_account_statement: false if the file is not a statement or activity export for a \
single bank/card account (then leave the other fields empty/null).
- account_last4: the last 4 digits of the account number printed for the statement \
(e.g. "3702-****-****-4474" -> "4474", "115280794" -> "0794"), digits only.
- statement_end_date / dates: YYYY-MM-DD. When a row's date has no year, take it from \
the statement period (a December row on a January statement is the previous year).
- opening_balance / closing_balance: the previous/opening balance and the new/closing \
balance exactly as printed (for a credit card, the amount owed). null if not printed.
- transactions: every line that changes the balance, in the order printed -- including \
interest, fees, and statement-level taxes (such as a "Total ITBMS" figure) that are part \
of the closing balance even if they only appear in a summary. Exclude balance lines, \
subtotals, and informational lines with a zero amount. amount is always positive; \
direction is "out" for money leaving a bank account or a charge on a card, "in" for \
money received, a payment to the card, or a refund/credit. card_last4: the last 4 \
digits of the card a row is listed under when the statement groups rows by card \
(e.g. under a "************2849" header), else null."""


class ExtractionError(Exception):
    """The file could not be turned into verified transactions."""


def is_configured() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def _content_block(filename: str, content: bytes) -> dict:
    if is_pdf(content):
        if len(content) > MAX_PDF_BYTES:
            raise ExtractionError(f'PDF is {len(content) // 1024 // 1024} MB, over the '
                                  f'{MAX_PDF_BYTES // 1024 // 1024} MB limit')
        return {'type': 'document', 'source': {
            'type': 'base64', 'media_type': 'application/pdf',
            'data': base64.b64encode(content).decode('ascii')}}
    if content[:2] == b'PK':                       # xlsx (zip)
        text = _xlsx_as_text(content)
    else:
        text = sniff_text(content)
        if '\x00' in text:
            raise ExtractionError('file is neither a PDF, a spreadsheet, nor text')
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(f'file has {len(text):,} characters, over the {MAX_TEXT_CHARS:,} limit')
    return {'type': 'document', 'title': filename,
            'source': {'type': 'text', 'media_type': 'text/plain', 'data': text}}


def _xlsx_as_text(content: bytes) -> str:
    import csv
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    except Exception as e:
        raise ExtractionError(f'could not open spreadsheet: {e}') from e
    out = io.StringIO()
    writer = csv.writer(out)
    for ws in wb.worksheets:
        out.write(f'# sheet: {ws.title}\n')
        for row in ws.iter_rows(values_only=True):
            if any(v is not None and v != '' for v in row):
                writer.writerow(['' if v is None else v for v in row])
    return out.getvalue()


def _call_model(filename: str, content: bytes) -> dict:
    import anthropic
    client = anthropic.Anthropic()
    try:
        # Streamed: a long statement can need a large output budget, which the
        # SDK won't send as a single blocking request.
        with client.beta.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            betas=['server-side-fallback-2026-07-01'],
            fallbacks='default',
            system=SYSTEM,
            output_config={'effort': 'medium',
                           'format': {'type': 'json_schema', 'schema': SCHEMA}},
            messages=[{'role': 'user', 'content': [
                _content_block(filename, content),
                {'type': 'text', 'text': f'Transcribe this file ("{filename}").'},
            ]}],
        ) as stream:
            resp = stream.get_final_message()
    except anthropic.APIStatusError as e:
        raise ExtractionError(f'Claude API error {e.status_code}: {e.message}') from e
    except anthropic.APIConnectionError as e:
        raise ExtractionError(f'could not reach the Claude API: {e}') from e

    if resp.stop_reason == 'refusal':
        raise ExtractionError('the model declined to read this file')
    if resp.stop_reason == 'max_tokens':
        raise ExtractionError('statement too long to extract in one pass')
    text = next((b.text for b in resp.content if b.type == 'text'), None)
    if not text:
        raise ExtractionError(f'no output (stop_reason={resp.stop_reason})')
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise ExtractionError(f'malformed JSON from the model: {e}') from e


def _date(s, what: str) -> datetime.date:
    try:
        return datetime.date.fromisoformat(s)
    except (TypeError, ValueError) as e:
        raise ExtractionError(f'{what} is not a YYYY-MM-DD date: {s!r}') from e


def to_parse_result(data: dict) -> ParseResult:
    """Validate the model's JSON and turn it into a reconciled ParseResult.
    Separate from the API call so it can be tested on its own."""
    if not data.get('is_account_statement'):
        raise ExtractionError('not a bank/card statement or activity export')
    if not any(c in (data.get('currency') or '').upper() for c in ('USD', 'US$', '$', 'DOLAR')):
        raise ExtractionError(f'currency {data.get("currency")!r} is not USD')

    stmt_account = accounts.ACCOUNT_BY_LAST4.get((data.get('account_last4') or '').strip())
    rows = []
    for i, t in enumerate(data.get('transactions') or []):
        amt = t.get('amount')
        if not isinstance(amt, (int, float)) or amt < 0:
            raise ExtractionError(f'row {i + 1}: amount {amt!r} is not a positive number')
        if amt == 0:
            continue
        account = accounts.ACCOUNT_BY_LAST4.get((t.get('card_last4') or '').strip()) or stmt_account
        if account is None:
            raise ExtractionError(
                f'account ...{data.get("account_last4")} (row card ...{t.get("card_last4")}) '
                'is not one of the household\'s known accounts')
        signed = round(amt if t['direction'] == 'out' else -amt, 2)
        desc = clean_desc(t.get('description') or '')
        rows.append(TxnRow(
            date=_date(t.get('date'), f'row {i + 1} date'), amount=signed,
            type='Purchase' if signed > 0 else (
                'Payment' if account in accounts.CARD_ACCOUNTS else 'Credit'),
            description=desc, merchant=desc, card=account))
    if not rows:
        raise ExtractionError('no transactions found')

    covered = sorted({r.card for r in rows})
    kinds = {r.card in accounts.CARD_ACCOUNTS for r in rows}
    if len(kinds) > 1:
        raise ExtractionError(f'rows span both card and deposit accounts: {", ".join(covered)}')
    is_card = kinds.pop()

    opening, closing = data.get('opening_balance'), data.get('closing_balance')
    if opening is None or closing is None:
        raise ExtractionError('no printed opening and closing balance to reconcile against')
    net_out = round(sum(r.amount for r in rows), 2)
    calc = round(opening + net_out if is_card else opening - net_out, 2)
    diff = round(abs(calc - closing), 2)
    ok = diff <= RECONCILE_TOLERANCE
    detail = (f'AI-extracted ({data.get("institution") or "unknown institution"}; no dedicated '
              f'parser for this format yet): {len(rows)} rows; opening {opening:.2f} '
              f'{"+" if is_card else "-"} net out {net_out:.2f} = {calc:.2f} vs closing '
              f'{closing:.2f}, diff {diff:.2f}')

    end = (_date(data['statement_end_date'], 'statement_end_date')
           if data.get('statement_end_date') else max(r.date for r in rows))
    return ParseResult(
        account_name=covered[0] if len(covered) == 1 else None, rows=rows,
        statement_period=end, period_label=f'{min(r.date for r in rows)}..{end}',
        reconciliation_ok=ok, reconciliation_detail=detail, accounts_covered=covered,
    )


def extract(filename: str, content: bytes) -> ParseResult:
    if not is_configured():
        raise ExtractionError('ANTHROPIC_API_KEY is not set on the pipeline service')
    data = _call_model(filename, content)
    logger.info('AI extraction for %s: %d row(s), account ...%s', filename,
                len(data.get('transactions') or []), data.get('account_last4'))
    return to_parse_result(data)
