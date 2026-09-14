#!/usr/bin/env python3
"""Household finance pipeline -- run-to-completion entrypoint.

Meant to be invoked on a schedule (Railway Cron Job service, e.g. daily),
NOT as a long-running server. On each run it:

  1. Lists files sitting in the Drive root folder "Johnson Suarez Financials".
  2. Skips any file already recorded in processed_statements as 'processed'.
  3. For each new/unresolved file: downloads it, detects which of the 13
     known account formats it matches, parses it, reconciles the parsed
     transactions against the statement's own printed totals, categorizes
     every row against category_keys (+ the special cases in categorize.py),
     writes processed_statements / transactions / needs_review rows, then
     archives the original into "Raw Originals (Archived)" and moves+renames
     it into "Source Documents (Standardized Names)".
  4. A file that fails to reconcile, fails to parse, or matches no known
     format is left exactly where it was dropped (Drive root) so a human
     notices it on their next look at the folder -- it is never guessed at.

See README.md for the full policy writeup and required environment
variables.
"""
from __future__ import annotations

import datetime
import sys
import traceback

import db
import drive_client
import naming
from categorize import Categorizer, NEEDS_REVIEW_THRESHOLD, UNCATEGORIZED
from parsers import parse_file
from parsers.common import UnrecognizedStatementError


def log(msg: str) -> None:
    print(f'[{datetime.datetime.now().isoformat(timespec="seconds")}] {msg}', flush=True)


def account_name_for_statement(result) -> str:
    if result.account_name:
        return result.account_name
    return ' & '.join(result.accounts_covered) if result.accounts_covered else 'Unknown account'


def process_one_file(drive_service, supabase, file_meta: dict, category_keys) -> None:
    file_id = file_meta['id']
    original_filename = file_meta['name']

    existing = db.get_processed_statement_by_drive_id(supabase, file_id)
    if existing and existing.get('status') == 'processed':
        log(f'SKIP (already processed): {original_filename}')
        return

    log(f'Downloading: {original_filename} ({file_id})')
    content = drive_client.download_file(drive_service, file_id)

    # ---- parse -----------------------------------------------------------
    try:
        result = parse_file(original_filename, content)
    except UnrecognizedStatementError as e:
        log(f'UNRECOGNIZED FORMAT: {original_filename}: {e}')
        _record_unprocessable(supabase, existing, file_id, original_filename,
                               status='needs_review',
                               detail=f'unrecognized statement format: {e}')
        return
    except Exception as e:
        log(f'PARSE ERROR: {original_filename}: {e}')
        traceback.print_exc()
        _record_unprocessable(supabase, existing, file_id, original_filename,
                               status='error',
                               detail=f'parser raised an exception: {e}')
        return

    account_name = account_name_for_statement(result)

    if not result.reconciliation_ok:
        log(f'RECONCILIATION FAILED: {original_filename}: {result.reconciliation_detail}')
        _record_unprocessable(supabase, existing, file_id, original_filename,
                               status='error', account_name=account_name,
                               statement_period=result.statement_period,
                               detail=result.reconciliation_detail,
                               row_count=len(result.rows))
        return

    log(f'Reconciled OK: {original_filename} ({len(result.rows)} rows) -- {result.reconciliation_detail}')

    # ---- categorize --------------------------------------------------------
    wise_giving, wise_invest = db.get_wise_category_counts(supabase)
    categorizer = Categorizer(category_keys, wise_giving, wise_invest)
    cats = categorizer.categorize_batch(result.rows)

    # ---- build standardized filename ---------------------------------------
    dates = [t.date for t in result.rows]
    standardized_name = naming.build_standardized_filename(
        start=min(dates), end=max(dates), account_names=result.accounts_covered or [account_name],
        extension=naming.extension_of(original_filename))

    # ---- write to Supabase --------------------------------------------------
    if existing:
        statement_id = existing['id']
        db.update_processed_statement(
            supabase, statement_id,
            standardized_filename=standardized_name, account_name=account_name,
            statement_period=result.statement_period.isoformat(), status='processed',
            reconciliation_ok=True, reconciliation_detail=result.reconciliation_detail,
            row_count=len(result.rows))
    else:
        stmt_row = db.insert_processed_statement(
            supabase, drive_file_id=file_id, original_filename=original_filename,
            standardized_filename=standardized_name, account_name=account_name,
            statement_period=result.statement_period, status='processed',
            reconciliation_ok=True, reconciliation_detail=result.reconciliation_detail,
            row_count=len(result.rows))
        statement_id = stmt_row['id']

    txn_dicts = []
    for row, (category, flow) in zip(result.rows, cats):
        txn_dicts.append({
            'date': row.date.isoformat(),
            'time': row.time,
            'cardholder': row.cardholder,
            'amount': row.amount,
            'points': row.points,
            'balance': row.balance,
            'status': row.status,
            'type': row.type,
            'merchant': row.merchant,
            'description': row.description,
            'card': row.card,
            'flow': flow,
            'category': category,
            'statement_id': statement_id,
        })

    inserted = db.insert_transactions(supabase, txn_dicts)
    log(f'Inserted {len(inserted)} transactions for {original_filename}')

    # ---- needs_review: significant uncategorized spend only -----------------
    # Read amount/category straight back off the rows Supabase echoed from
    # the insert, so this doesn't depend on response ordering matching
    # txn_dicts order.
    uncategorized = [(t, t['amount']) for t in inserted if t.get('category') == UNCATEGORIZED]
    # Note: the spec's "sum >= $50 OR any single row >= $50" collapses to
    # just the sum check, since sum(|amounts|) >= max(|amount|) always --
    # see categorize.py module docstring for the full reasoning. When it
    # triggers, every Uncategorized row from this statement is queued.
    total_uncategorized = sum(abs(a) for (_t, a) in uncategorized)
    if uncategorized and total_uncategorized >= NEEDS_REVIEW_THRESHOLD:
        review_rows = [{
            'transaction_id': t['id'],
            'statement_id': statement_id,
            'reason': f'unrecognized merchant, ${abs(a):.2f} uncategorized',
            'amount': a,
            'merchant': t.get('merchant'),
            'description': t.get('description'),
            'status': 'open',
        } for (t, a) in uncategorized]
        db.insert_needs_review(supabase, review_rows)
        log(f'Queued {len(review_rows)} needs_review rows (${total_uncategorized:.2f} uncategorized total)')

    # ---- archive in Drive ----------------------------------------------------
    try:
        drive_client.copy_to_raw_originals(drive_service, file_id, original_filename)
        drive_client.move_and_rename_to_standardized(drive_service, file_id, standardized_name)
        log(f'Archived + renamed in Drive: "{original_filename}" -> "{standardized_name}"')
    except Exception as e:
        log(f'WARNING: DB writes for {original_filename} succeeded, but Drive archive/rename '
            f'failed: {e}. The file remains in the root folder; processed_statements already '
            f'shows it as processed, so re-running the pipeline will NOT reprocess it. Move/'
            f'rename it manually in Drive, or clear its processed_statements row to retry.')
        traceback.print_exc()


def _record_unprocessable(supabase, existing, file_id, original_filename, *, status, detail,
                           account_name=None, statement_period=None, row_count=None):
    if existing:
        db.update_processed_statement(
            supabase, existing['id'], status=status, reconciliation_ok=False,
            reconciliation_detail=detail, account_name=account_name,
            statement_period=(statement_period.isoformat() if statement_period else None),
            row_count=row_count)
        statement_id = existing['id']
    else:
        stmt_row = db.insert_processed_statement(
            supabase, drive_file_id=file_id, original_filename=original_filename,
            standardized_filename=None, account_name=account_name,
            statement_period=statement_period, status=status, reconciliation_ok=False,
            reconciliation_detail=detail, row_count=row_count)
        statement_id = stmt_row['id']

    if status == 'needs_review':
        db.insert_needs_review(supabase, [{
            'transaction_id': None,
            'statement_id': statement_id,
            'reason': detail[:500],
            'amount': None,
            'merchant': original_filename,
            'description': detail[:500],
            'status': 'open',
        }])


def main() -> int:
    log('Pipeline run starting')
    supabase = db.get_client()
    drive_service = drive_client.get_service()

    category_keys = db.get_active_category_keys(supabase)
    log(f'Loaded {len(category_keys)} active category_keys rows')

    files = drive_client.list_root_files(drive_service)
    log(f'Found {len(files)} file(s) in Drive root folder')

    if not files:
        log('Nothing to do.')
        return 0

    errors = 0
    for file_meta in files:
        try:
            process_one_file(drive_service, supabase, file_meta, category_keys)
        except Exception as e:
            errors += 1
            log(f'UNEXPECTED ERROR processing {file_meta.get("name")}: {e}')
            traceback.print_exc()

    log(f'Pipeline run complete. {errors} file(s) hit unexpected errors.')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
