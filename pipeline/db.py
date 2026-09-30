"""Supabase access for the pipeline service, via the service_role key
(bypasses RLS -- this process is trusted, unattended, and runs on a
schedule with no end user attached to a session).

Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from the environment. Never
hardcode a URL or key here -- see README.md for where to get each value.
"""
from __future__ import annotations

import os
from typing import Optional

from supabase import create_client, Client


def get_client() -> Client:
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL and SUPABASE_SERVICE_KEY must be set in the environment. '
            'See README.md "Environment variables".')
    return create_client(url, key)


def get_active_category_keys(client: Client) -> list:
    resp = (client.table('category_keys')
            .select('id, category, flow, keywords, priority, notes')
            .eq('active', True)
            .order('priority')
            .order('id')
            .execute())
    return resp.data or []


def get_processed_statement_by_drive_id(client: Client, drive_file_id: str) -> Optional[dict]:
    resp = (client.table('processed_statements')
            .select('*')
            .eq('drive_file_id', drive_file_id)
            .limit(1)
            .execute())
    rows = resp.data or []
    return rows[0] if rows else None


def insert_processed_statement(client: Client, *, drive_file_id: str, original_filename: str,
                                standardized_filename: Optional[str], account_name: Optional[str],
                                statement_period, status: str, reconciliation_ok: Optional[bool],
                                reconciliation_detail: Optional[str], row_count: Optional[int]) -> dict:
    resp = client.table('processed_statements').insert({
        'drive_file_id': drive_file_id,
        'original_filename': original_filename,
        'standardized_filename': standardized_filename,
        'account_name': account_name,
        'statement_period': statement_period.isoformat() if statement_period else None,
        'status': status,
        'reconciliation_ok': reconciliation_ok,
        'reconciliation_detail': reconciliation_detail,
        'row_count': row_count,
    }).execute()
    return resp.data[0]


def update_processed_statement(client: Client, statement_id: str, **fields):
    client.table('processed_statements').update(fields).eq('id', statement_id).execute()


def insert_transactions(client: Client, rows: list) -> list:
    """Bulk-insert transaction dicts; returns the inserted rows (with ids).
    Supabase/PostgREST caps request size, so chunk defensively."""
    inserted = []
    CHUNK = 500
    for i in range(0, len(rows), CHUNK):
        chunk = rows[i:i + CHUNK]
        resp = client.table('transactions').insert(chunk).execute()
        inserted.extend(resp.data or [])
    return inserted


def get_transaction_date_amounts(client: Client, card: str, start, end) -> list:
    """(date ISO string, amount) of every existing transaction on ``card``
    between ``start`` and ``end`` inclusive, paged past PostgREST's row cap."""
    out = []
    PAGE = 1000
    offset = 0
    while True:
        resp = (client.table('transactions')
                .select('date, amount')
                .eq('card', card)
                .gte('date', start.isoformat())
                .lte('date', end.isoformat())
                .order('id')
                .range(offset, offset + PAGE - 1)
                .execute())
        rows = resp.data or []
        out.extend((r['date'], round(float(r['amount']), 2)) for r in rows)
        if len(rows) < PAGE:
            return out
        offset += PAGE


def has_open_statement_review(client: Client, statement_id: str) -> bool:
    """Whether an open, file-level (no transaction) review item already
    exists for this statement."""
    resp = (client.table('needs_review')
            .select('id')
            .eq('statement_id', statement_id)
            .is_('transaction_id', 'null')
            .eq('status', 'open')
            .limit(1)
            .execute())
    return bool(resp.data)


def insert_needs_review(client: Client, rows: list) -> list:
    if not rows:
        return []
    resp = client.table('needs_review').insert(rows).execute()
    return resp.data or []


def get_wise_category_counts(client: Client) -> tuple:
    """Count of existing transactions already tagged as the two Wise-split
    categories, used to seed the alternation so it stays balanced across
    separate pipeline runs (not just within one statement)."""
    giving = (client.table('transactions')
              .select('id', count='exact')
              .eq('category', 'Charitable giving')
              .ilike('description', '%WISE%')
              .execute())
    invest = (client.table('transactions')
              .select('id', count='exact')
              .eq('category', 'Investment')
              .ilike('description', '%WISE%')
              .execute())
    return (giving.count or 0), (invest.count or 0)
