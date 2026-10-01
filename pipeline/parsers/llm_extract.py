"""AI reader: turns any statement-like file into transactions.

The household's priority is that whatever gets dropped in the folder is
processed -- a statement PDF, a CSV/XLSX export, an online-banking page
printed to PDF, a screenshot or photo. Dedicated parsers handle the known
formats for free; this module handles everything else, and also re-reads a
file whose dedicated parser broke (``parsers.parse_file``).

Claude (strict JSON schema) reports what is printed: which of the
household's accounts it is (by printed account/card digits, else by
choosing from the account list), each row (date, description, amount,
direction, card it's listed under), and opening/closing balances if any.
The pipeline then decides how far to trust it:

- **Verified** -- opening and closing balances printed and the rows bridge
  them within ``RECONCILE_TOLERANCE`` (deposit: ``opening - out + in``;
  card, balance = amount owed: ``opening + out - in``).
- **Unverified** -- no balances printed (activity listings, screen
  printouts). Booked anyway; the detail says so. Duplicates are still
  caught by main.py's ledger dedupe.
- **Mismatch** -- balances printed but the rows don't bridge them: the file
  is re-read once with the discrepancy pointed out; only if it still
  doesn't add up is the result returned as not-OK (-> Review).

``ExtractionError`` is raised only when nothing usable came back (not a
financial document, no rows, account not identifiable, API failure).
Every detail string carries ``VERSION_TAG``; main.py won't re-send a file
that failed under the current version, and bumping the tag makes files
that failed under older logic get retried automatically.
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
MAX_IMAGE_BYTES = 4 * 1024 * 1024       # API limit is 5 MB; leave headroom
MAX_IMAGE_EDGE = 4000
MAX_TEXT_CHARS = 400_000
VERSION_TAG = '[ai v2]'
AI_FAILED_MARKER = f'AI extraction failed {VERSION_TAG}'
UNKNOWN_ACCOUNT = 'unknown'

# What each account looks like on paper, so the model can pick one when no
# account number is printed (e.g. Robinhood exports).
ACCOUNT_HINTS = {
    accounts.PANAMA_DEBIT: 'BAC Credomatic (Panama) debit/checking account 115280794',
    accounts.CITI_AADVANTAGE: 'Citi AAdvantage Platinum credit card',
    accounts.CAPONE_QUICKSILVER: 'Capital One Quicksilver credit card',
    accounts.CITI_COSTCO: 'Citi Costco Anywhere Visa credit card',
    accounts.HUNTINGTON_CHECKING: 'Huntington Bank checking account',
    accounts.AMEX_CONNECTMILES: 'BAC Credomatic AMEX ConnectMiles credit card (account 3702-...-4474)',
    accounts.BANCO_GENERAL_TRANSFERS: 'Banco General (Panama) savings account / Yappy / Banca Movil',
    accounts.ROBINHOOD_SPENDING: 'Robinhood bank / spending / savings account (not the card)',
    accounts.CAPONE_360_SAVINGS: 'Capital One 360 Savings',
    accounts.PANAMA_MASTERCARD_2849: 'BAC Credomatic Mastercard 5536-20**-****-2849 (Sandra)',
    accounts.PANAMA_MASTERCARD_3029: 'BAC Credomatic Mastercard 5536-20**-****-3029 (Trevor)',
    accounts.CAPONE_360_CHECKING: 'Capital One 360 Checking',
    accounts.ROBINHOOD_VISA: 'Robinhood credit card (Robinhood Gold Card / Visa)',
}

SCHEMA = {
    'type': 'object',
    'properties': {
        'is_account_statement': {'type': 'boolean'},
        'institution': {'type': 'string'},
        'account_last4': {'type': ['string', 'null']},
        'account': {'type': 'string', 'enum': list(accounts.ALL) + [UNKNOWN_ACCOUNT]},
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
    'required': ['is_account_statement', 'institution', 'account_last4', 'account', 'currency',
                 'statement_end_date', 'opening_balance', 'closing_balance', 'transactions'],
    'additionalProperties': False,
}

SYSTEM = """You transcribe bank and credit-card statements, account-activity exports, \
online-banking pages and screenshots into JSON, exactly as printed. A separate program checks your numbers against the printed \
balances, so accuracy matters more than completeness of anything else: never estimate, \
infer, or invent a value.

- is_account_statement: false only if the file shows no bank/card transactions at all \
(then leave the other fields empty/null).
- account: which of the household's accounts this is (list below), chosen from the \
account/card numbers, institution and product name shown; "unknown" if it matches none.
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
(e.g. under a "************2849" or "5536-20**-****-2849" header), else null.
- If the file shows pending/in-transit ("en tránsito", "flotantes") items separately from \
processed ones, include only processed/posted rows.

The household's accounts:
""" + "\n".join(f"- {name}: {hint}" for name, hint in ACCOUNT_HINTS.items())


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
    image = _image_block(content)
    if image is not None:
        return image
    if content[:2] == b'PK':                       # xlsx (zip)
        text = _xlsx_as_text(content)
    else:
        text = sniff_text(content)
        if '\x00' in text:
            raise ExtractionError('file is not a PDF, image, spreadsheet, or text')
    if len(text) > MAX_TEXT_CHARS:
        raise ExtractionError(f'file has {len(text):,} characters, over the {MAX_TEXT_CHARS:,} limit')
    return {'type': 'document', 'title': filename,
            'source': {'type': 'text', 'media_type': 'text/plain', 'data': text}}


def _image_kind(content: bytes):
    if content[:8] == b'\x89PNG\r\n\x1a\n':
        return 'image/png'
    if content[:3] == b'\xff\xd8\xff':
        return 'image/jpeg'
    if content[:6] in (b'GIF87a', b'GIF89a'):
        return 'image/gif'
    if content[:4] == b'RIFF' and content[8:12] == b'WEBP':
        return 'image/webp'
    if content[4:12] in (b'ftypheic', b'ftypheix', b'ftypmif1', b'ftypmsf1', b'ftypheif', b'ftyphevc'):
        return 'image/heic'
    return None


def _image_block(content: bytes):
    """Screenshots/photos. HEIC (iPhone photos) and anything over the API's
    size limits is converted/shrunk to JPEG first."""
    kind = _image_kind(content)
    if kind is None:
        return None
    if kind == 'image/heic' or len(content) > MAX_IMAGE_BYTES:
        content, kind = _to_jpeg(content), 'image/jpeg'
    return {'type': 'image', 'source': {
        'type': 'base64', 'media_type': kind, 'data': base64.b64encode(content).decode('ascii')}}


def _to_jpeg(content: bytes) -> bytes:
    try:
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        img = Image.open(io.BytesIO(content)).convert('RGB')
    except Exception as e:
        raise ExtractionError(f'could not open image: {e}') from e
    img.thumbnail((MAX_IMAGE_EDGE, MAX_IMAGE_EDGE))
    for quality in (90, 80, 70, 60):
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=quality)
        if out.tell() <= MAX_IMAGE_BYTES:
            return out.getvalue()
        img.thumbnail((img.width * 3 // 4, img.height * 3 // 4))
    return out.getvalue()


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


def _call_model(filename: str, content: bytes, feedback: str | None = None) -> dict:
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
                {'type': 'text', 'text': f'Transcribe this file ("{filename}").'
                 + (f'\n\n{feedback}' if feedback else '')},
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
    """Validate the model's JSON and turn it into a ParseResult (verified,
    unverified, or mismatch -- see the module docstring). Separate from the
    API call so it can be tested on its own."""
    if not data.get('is_account_statement'):
        raise ExtractionError('no bank/card transactions found in this file')
    if not any(c in (data.get('currency') or '').upper() for c in ('USD', 'US$', '$', 'DOLAR')):
        raise ExtractionError(f'currency {data.get("currency")!r} is not USD')

    chosen = data.get('account') if data.get('account') in accounts.ALL else None
    stmt_account = accounts.ACCOUNT_BY_LAST4.get((data.get('account_last4') or '').strip()) or chosen
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
                f'could not tell which account this is (printed ...{data.get("account_last4")}, '
                f'institution {data.get("institution")!r}); add it to accounts.ACCOUNT_HINTS')
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
    source = f'AI-extracted {VERSION_TAG} ({data.get("institution") or "unknown institution"})'
    opening, closing = data.get('opening_balance'), data.get('closing_balance')
    if opening is None or closing is None or len(kinds) > 1:
        ok = True
        why = ('rows span card and deposit accounts' if len(kinds) > 1
               else 'no opening and closing balance printed')
        detail = f'{source}: {len(rows)} rows; UNVERIFIED ({why}; duplicates still skipped)'
    else:
        is_card = kinds.pop()
        net_out = round(sum(r.amount for r in rows), 2)
        calc = round(opening + net_out if is_card else opening - net_out, 2)
        diff = round(abs(calc - closing), 2)
        ok = diff <= RECONCILE_TOLERANCE
        detail = (f'{source}: {len(rows)} rows; opening {opening:.2f} '
                  f'{"+" if is_card else "-"} net out {net_out:.2f} = {calc:.2f} vs closing '
                  f'{closing:.2f}, diff {diff:.2f}' + ('' if ok else ' -- DOES NOT ADD UP'))

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
    logger.info('AI extraction for %s: %d row(s), account %s', filename,
                len(data.get('transactions') or []), data.get('account'))
    result = to_parse_result(data)
    if result.reconciliation_ok:
        return result

    # Printed balances but the rows don't bridge them: usually a missed or
    # misread line. Re-read once with the discrepancy pointed out.
    logger.warning('AI extraction for %s does not add up, re-reading: %s', filename,
                   result.reconciliation_detail)
    feedback = (f'A previous transcription of this file did not add up: '
                f'{result.reconciliation_detail.split(": ", 1)[-1]}. Re-read every line carefully '
                '-- look for missed, duplicated or misread rows, amounts, and in/out directions, '
                'and fees/taxes/interest that are part of the closing balance.')
    retry = to_parse_result(_call_model(filename, content, feedback=feedback))
    if retry.reconciliation_ok:
        retry.reconciliation_detail += ' (second reading)'
    return retry
