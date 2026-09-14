"""Builds the downloadable export files for the /export page.

The household used to keep one Excel workbook as the source of truth and
could hand a copy of it to any tool (including an LLM) for further
analysis. Now that Supabase is the source of truth, this module is what
replaces that capability -- a plain CSV of the full transaction ledger
(the direct equivalent of the old workbook's main sheet, and the most
broadly compatible format for spreadsheets and LLMs alike) plus a full
JSON bundle of every table for a complete backup/analysis snapshot.
"""
from __future__ import annotations

import csv
import io
import json

TRANSACTIONS_CSV_COLUMNS = [
    'date', 'time', 'cardholder', 'amount', 'points', 'balance', 'status',
    'type', 'merchant', 'description', 'card', 'flow', 'category',
]


def transactions_csv(rows: list) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=TRANSACTIONS_CSV_COLUMNS, extrasaction='ignore')
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


def full_bundle_json(bundle: dict) -> str:
    return json.dumps(bundle, indent=2, default=str)
