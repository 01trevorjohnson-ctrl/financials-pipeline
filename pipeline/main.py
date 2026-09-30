#!/usr/bin/env python3
"""Household finance pipeline.

Two ways this gets run:

  1. On a schedule (Railway **Cron Job** service): ``python -m pipeline.main``
     from the repo root. The ``if __name__ == "__main__":`` block at the
     bottom calls :func:`run_pipeline`, prints a human-readable summary in
     the same style the household already checks in Railway's cron logs,
     and exits with the right code.
  2. On demand, imported by the dashboard's "Run Pipeline Now" button:
     ``from pipeline.main import run_pipeline``. :func:`run_pipeline` never
     calls ``print()``/``sys.exit()`` -- only structured logging (the
     stdlib ``logging`` module) and its return value -- so it is safe to
     call synchronously from a long-running web process.

Both entrypoints run the exact same logic: list the Drive root folder,
skip anything already recorded in ``processed_statements`` as
``'processed'``, and for each new/unresolved file: download it, detect
which of the known account formats it matches, parse it, reconcile the
parsed transactions against the statement's own printed totals, categorize
every row against ``category_keys`` (+ the special cases in
``categorize.py``), write ``processed_statements``/``transactions``/
``needs_review`` rows, then move+rename it into "Source Documents
(Standardized Names)" (there is no automated copy into "Raw Originals
(Archived)" -- see the note above ``move_and_rename_to_standardized``'s call
site for why: Drive service accounts cannot create new file content on a
personal Drive at all). A file that fails to reconcile, fails to parse, or
matches no known format is left exactly where it was dropped (Drive root) so
a human notices it -- it is never guessed at.

See README.md for the full policy writeup and required environment
variables.
"""
from __future__ import annotations

import dataclasses
import datetime
import logging
import sys
import traceback
from typing import List

from . import db
from . import drive_client
from . import naming
from .categorize import Categorizer, NEEDS_REVIEW_THRESHOLD, UNCATEGORIZED, llm_categorize_uncategorized
from .parsers import llm_extract, parse_file
from .parsers.common import UnrecognizedStatementError

logger = logging.getLogger(__name__)


@dataclasses.dataclass
class FileRunResult:
    """Outcome of attempting to process a single Drive file."""
    filename: str
    drive_file_id: str
    outcome: str  # 'processed' | 'skipped' | 'failed'
    detail: str = ''
    transactions_inserted: int = 0
    needs_review_queued: int = 0


@dataclasses.dataclass
class PipelineRunResult:
    """Everything a caller (the cron __main__ block, or the dashboard's
    "Run Pipeline Now" button) needs to know about one pipeline run."""
    started_at: datetime.datetime
    finished_at: datetime.datetime
    files_found: int = 0
    files: List[FileRunResult] = dataclasses.field(default_factory=list)
    # Truly unexpected exceptions that escaped process_one_file entirely
    # (as opposed to parse/reconciliation failures, which are expected,
    # handled outcomes -- see files_failed below).
    unexpected_errors: int = 0

    @property
    def files_processed(self) -> int:
        return sum(1 for f in self.files if f.outcome == 'processed')

    @property
    def files_skipped(self) -> int:
        return sum(1 for f in self.files if f.outcome == 'skipped')

    @property
    def files_failed(self) -> List[FileRunResult]:
        """Files that failed to parse, failed reconciliation, matched no
        known format, or hit an unexpected error -- i.e. anything left in
        the Drive root for a human to look at."""
        return [f for f in self.files if f.outcome == 'failed']

    @property
    def total_transactions_inserted(self) -> int:
        return sum(f.transactions_inserted for f in self.files)

    @property
    def total_needs_review_queued(self) -> int:
        return sum(f.needs_review_queued for f in self.files)

    @property
    def ok(self) -> bool:
        """Matches the ORIGINAL script's exit-code contract exactly: only
        truly unexpected errors make this a non-zero-exit ("something is
        broken") run. A file that failed to reconcile/parse is expected,
        handled, business-as-usual behavior (see README) and does NOT by
        itself make the run 'not ok' for cron-exit-code purposes -- it's
        surfaced instead via files_failed for humans to review."""
        return self.unexpected_errors == 0

    def summary_lines(self) -> List[str]:
        """Human-readable lines, matching the pipeline's historical log
        style, for both the cron __main__ block and the dashboard's Home
        page."""
        lines = [
            f'Pipeline run: {self.started_at.isoformat(timespec="seconds")} -> '
            f'{self.finished_at.isoformat(timespec="seconds")}',
            f'Found {self.files_found} file(s) in Drive root folder.',
            f'Processed OK: {self.files_processed}   '
            f'Skipped (already processed): {self.files_skipped}   '
            f'Failed: {len(self.files_failed)}',
            f'Transactions inserted: {self.total_transactions_inserted}   '
            f'needs_review rows queued: {self.total_needs_review_queued}',
        ]
        for f in self.files_failed:
            lines.append(f'  FAILED: {f.filename} -- {f.detail}')
        if self.unexpected_errors:
            lines.append(f'{self.unexpected_errors} file(s) hit UNEXPECTED errors -- see logs above.')
        return lines


def account_name_for_statement(result) -> str:
    if result.account_name:
        return result.account_name
    return ' & '.join(result.accounts_covered) if result.accounts_covered else 'Unknown account'


def drop_rows_already_in_ledger(supabase, result):
    """Drop parsed rows whose (date, amount) is already in ``transactions``
    for the same account, so a file that overlaps earlier uploads (a
    rolling export, a re-downloaded statement, a PDF of a month already
    loaded from CSV) is processed for its new rows instead of
    double-inserting. Runs for every format, after reconciliation (which
    checks the whole file as printed). Matched as a multiset -- two
    identical $50 rows on one day in the file with one already in the
    ledger keeps exactly one. Descriptions aren't compared, since the same
    movement is worded differently across source formats (CSV vs PDF, and
    the historical backfills).

    Returns (new ParseResult, number of rows skipped); the reconciliation
    detail is extended to say how many were skipped.
    """
    existing = {}
    for card in {r.card for r in result.rows}:
        card_dates = [r.date for r in result.rows if r.card == card]
        for d, amt in db.get_transaction_date_amounts(supabase, card, min(card_dates), max(card_dates)):
            key = (card, d, amt)
            existing[key] = existing.get(key, 0) + 1

    kept = []
    for r in result.rows:
        key = (r.card, r.date.isoformat(), round(r.amount, 2))
        if existing.get(key):
            existing[key] -= 1
        else:
            kept.append(r)

    skipped = len(result.rows) - len(kept)
    if not skipped:
        return result, 0
    detail = (f'{result.reconciliation_detail} {skipped} row(s) already in the ledger '
              f'(overlap with an earlier export) skipped; {len(kept)} new.')
    return dataclasses.replace(result, rows=kept, reconciliation_detail=detail), skipped


def process_one_file(drive_service, supabase, file_meta: dict, category_keys) -> FileRunResult:
    file_id = file_meta['id']
    original_filename = file_meta['name']

    existing = db.get_processed_statement_by_drive_id(supabase, file_id)
    if existing and existing.get('status') == 'processed':
        logger.info('SKIP (already processed): %s', original_filename)
        return FileRunResult(original_filename, file_id, 'skipped', detail='already processed')

    logger.info('Downloading: %s (%s)', original_filename, file_id)
    content = drive_client.download_file(drive_service, file_id, file_meta.get('mimeType'))

    # ---- parse -----------------------------------------------------------
    # The AI fallback costs an API call per try; a file it already failed on
    # (couldn't extract, or extracted rows that didn't reconcile) stays in
    # the Drive root, so don't re-send it on every scheduled run.
    prior = (existing or {}).get('reconciliation_detail') or ''
    allow_llm = not (llm_extract.AI_FAILED_MARKER in prior or prior.startswith('AI-extracted'))
    try:
        result = parse_file(original_filename, content, allow_llm=allow_llm)
    except UnrecognizedStatementError as e:
        logger.warning('UNRECOGNIZED FORMAT: %s: %s', original_filename, e)
        detail = f'unrecognized statement format: {e}'
        queued = _record_unprocessable(supabase, existing, file_id, original_filename,
                                        status='needs_review', detail=detail)
        return FileRunResult(original_filename, file_id, 'failed', detail=detail,
                              needs_review_queued=queued)
    except Exception as e:
        logger.error('PARSE ERROR: %s: %s', original_filename, e)
        logger.error(traceback.format_exc())
        detail = f'parser raised an exception: {e}'
        queued = _record_unprocessable(supabase, existing, file_id, original_filename,
                                        status='error', detail=detail)
        return FileRunResult(original_filename, file_id, 'failed', detail=detail,
                              needs_review_queued=queued)

    account_name = account_name_for_statement(result)

    if not result.reconciliation_ok:
        logger.warning('RECONCILIATION FAILED: %s: %s', original_filename, result.reconciliation_detail)
        queued = _record_unprocessable(supabase, existing, file_id, original_filename,
                                        status='error', account_name=account_name,
                                        statement_period=result.statement_period,
                                        detail=result.reconciliation_detail,
                                        row_count=len(result.rows))
        return FileRunResult(original_filename, file_id, 'failed',
                              detail=result.reconciliation_detail or 'reconciliation failed',
                              needs_review_queued=queued)

    logger.info('Reconciled OK: %s (%d rows) -- %s', original_filename, len(result.rows),
                result.reconciliation_detail)

    # Naming uses the file's full parsed span, even if dedupe below drops rows.
    dates = [t.date for t in result.rows]

    # ---- skip rows already in the ledger (overlapping uploads) ---------------
    result, skipped = drop_rows_already_in_ledger(supabase, result)
    if skipped:
        logger.info('Skipped %d row(s) already in the ledger for %s', skipped, original_filename)

    # ---- categorize --------------------------------------------------------
    wise_giving, wise_invest = db.get_wise_category_counts(supabase)
    categorizer = Categorizer(category_keys, wise_giving, wise_invest)
    cats = categorizer.categorize_batch(result.rows)

    # Transfers to individuals no specific keyword identifies: never guessed
    # (keyword catch-all or LLM) -- left Uncategorized and always reviewed.
    person_flags = [categorizer.is_unconfirmed_transfer_to_individual(r.merchant, r.description, r.amount)
                    for r in result.rows]
    for i, flagged in enumerate(person_flags):
        if flagged:
            cats[i] = (UNCATEGORIZED, 'Spend')    # outgoing, to a person -- never 'CC payment'

    # ---- LLM fallback for rows no keyword matched ---------------------------
    # One batched call per statement, covering every row categorize_batch left
    # as Uncategorized. Never blocks/fails the run -- see
    # llm_categorize_uncategorized's docstring for the full degrade policy
    # (missing ANTHROPIC_API_KEY, a network error, or a bad response all just
    # mean these rows keep their existing Uncategorized + needs_review result).
    category_flow_map = {}
    for ck in category_keys:
        category_flow_map.setdefault(ck['category'], ck['flow'])
    uncategorized_indices = [(i, result.rows[i]) for i, (cat, _flow) in enumerate(cats)
                              if cat == UNCATEGORIZED and not person_flags[i]]
    if uncategorized_indices:
        llm_results = llm_categorize_uncategorized(uncategorized_indices, category_flow_map)
        if llm_results:
            logger.info('LLM categorized %d/%d keyword-unmatched row(s) for %s',
                        len(llm_results), len(uncategorized_indices), original_filename)
        for i, (category, flow) in llm_results.items():
            cats[i] = (category, flow)

    # ---- build standardized filename ---------------------------------------
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
    logger.info('Inserted %d transactions for %s', len(inserted), original_filename)

    # ---- needs_review: transfers to individuals + significant uncategorized --
    # Read amount/category straight back off the rows Supabase echoed from
    # the insert, so this doesn't depend on response ordering matching
    # txn_dicts order. Flagged person transfers are picked out of the echo by
    # (date, amount, description), as a multiset.
    person_keys = {}
    for row, flagged in zip(result.rows, person_flags):
        if flagged:
            key = (row.date.isoformat(), round(row.amount, 2), row.description)
            person_keys[key] = person_keys.get(key, 0) + 1
    person_transfers, uncategorized = [], []
    for t in inserted:
        key = (t.get('date'), round(float(t['amount']), 2), t.get('description'))
        if person_keys.get(key):
            person_keys[key] -= 1
            person_transfers.append((t, t['amount']))
        elif t.get('category') == UNCATEGORIZED:
            uncategorized.append((t, t['amount']))
    # Note: the spec's "sum >= $50 OR any single row >= $50" collapses to
    # just the sum check, since sum(|amounts|) >= max(|amount|) always --
    # see categorize.py module docstring for the full reasoning. When it
    # triggers, every Uncategorized row from this statement is queued.
    total_uncategorized = sum(abs(a) for (_t, a) in uncategorized)
    review_rows = [
        (t, a, f'transfer to an individual, ${abs(a):.2f} -- choose a category')
        for (t, a) in person_transfers]
    if uncategorized and total_uncategorized >= NEEDS_REVIEW_THRESHOLD:
        review_rows += [(t, a, f'unrecognized merchant, ${abs(a):.2f} uncategorized')
                        for (t, a) in uncategorized]
    needs_review_queued = 0
    if review_rows:
        db.insert_needs_review(supabase, [{
            'transaction_id': t['id'],
            'statement_id': statement_id,
            'reason': reason,
            'amount': a,
            'merchant': t.get('merchant'),
            'description': t.get('description'),
            'status': 'open',
        } for (t, a, reason) in review_rows])
        needs_review_queued = len(review_rows)
        logger.info('Queued %d needs_review rows (%d transfer(s) to individuals; $%.2f other uncategorized)',
                    needs_review_queued, len(person_transfers), total_uncategorized)

    # ---- move + rename in Drive ------------------------------------------------
    # NOT a copy-then-move: Google Drive service accounts have zero storage
    # quota on a personal (non-Workspace) Drive and cannot create ANY new file
    # content -- files().copy() (and any upload) fails with 403
    # storageQuotaExceeded, unconditionally, regardless of which folder it
    # targets. There is no per-request workaround; Google's own fix for this
    # is Shared Drives or domain-wide delegation, both Workspace-only features
    # this account doesn't have. So there is no automated "Raw Originals
    # (Archived)" duplicate for pipeline-processed files -- only a single
    # move+rename into "Source Documents (Standardized Names)", which is a
    # pure metadata operation (addParents/removeParents/rename) and needs no
    # quota. See README.md for the full explanation.
    drive_warning = None
    try:
        drive_client.move_and_rename_to_standardized(drive_service, file_id, standardized_name)
        logger.info('Moved + renamed in Drive: "%s" -> "%s"', original_filename, standardized_name)
    except Exception as e:
        drive_warning = (
            f'DB writes for {original_filename} succeeded, but the Drive move/rename failed: {e}. '
            'The file remains in the root folder; processed_statements already shows it as '
            'processed, so re-running the pipeline will NOT reprocess it. Move/rename it manually '
            'in Drive, or clear its processed_statements row to retry.')
        logger.warning(drive_warning)
        logger.warning(traceback.format_exc())

    detail = f'{len(inserted)} transaction(s) inserted'
    if drive_warning:
        detail += f'; WARNING: {drive_warning}'
    return FileRunResult(original_filename, file_id, 'processed', detail=detail,
                          transactions_inserted=len(inserted), needs_review_queued=needs_review_queued)


def _record_unprocessable(supabase, existing, file_id, original_filename, *, status, detail,
                           account_name=None, statement_period=None, row_count=None) -> int:
    """Writes the processed_statements (+ optional needs_review) rows for a
    file that could not be processed. Returns the number of needs_review
    rows queued (0 or 1)."""
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

    # One open review item per file: an unrecognized file stays in the Drive
    # root and is seen again every run, which used to queue a duplicate.
    if status == 'needs_review' and not db.has_open_statement_review(supabase, statement_id):
        db.insert_needs_review(supabase, [{
            'transaction_id': None,
            'statement_id': statement_id,
            'reason': detail[:500],
            'amount': None,
            'merchant': original_filename,
            'description': detail[:500],
            'status': 'open',
        }])
        return 1
    return 0


def _record_unexpected_error(supabase, file_meta: dict, error: Exception) -> None:
    """Best effort: leave a processed_statements row saying why a file
    crashed, so it shows up next to the other failures instead of only in
    the service logs. Never raises. Skips files already recorded as
    processed, so a crash after a successful insert can't hide that."""
    try:
        existing = db.get_processed_statement_by_drive_id(supabase, file_meta['id'])
        if existing and existing.get('status') == 'processed':
            return
        _record_unprocessable(supabase, existing, file_meta['id'], file_meta.get('name', '(unknown)'),
                              status='error', detail=f'unexpected error: {error}'[:1000])
    except Exception:
        logger.error('Could not record the unexpected error for %s', file_meta.get('name'))
        logger.error(traceback.format_exc())


def run_pipeline() -> PipelineRunResult:
    """Run one full pipeline pass and return a :class:`PipelineRunResult`.

    Contains ALL of the actual pipeline logic. Never prints or calls
    sys.exit() -- only structured logging (module-level ``logger``) and its
    return value -- so it's safe to call from a long-running process (e.g.
    the dashboard's "Run Pipeline Now" button) as well as from the cron
    entrypoint below.
    """
    started_at = datetime.datetime.now(datetime.timezone.utc)
    logger.info('Pipeline run starting')

    supabase = db.get_client()
    drive_service = drive_client.get_service()

    category_keys = db.get_active_category_keys(supabase)
    logger.info('Loaded %d active category_keys rows', len(category_keys))

    files = drive_client.list_root_files(drive_service)
    logger.info('Found %d file(s) in Drive root folder', len(files))

    result = PipelineRunResult(started_at=started_at, finished_at=started_at, files_found=len(files))

    for file_meta in files:
        try:
            result.files.append(process_one_file(drive_service, supabase, file_meta, category_keys))
        except Exception as e:
            result.unexpected_errors += 1
            logger.error('UNEXPECTED ERROR processing %s: %s', file_meta.get('name'), e)
            logger.error(traceback.format_exc())
            _record_unexpected_error(supabase, file_meta, e)
            result.files.append(FileRunResult(
                filename=file_meta.get('name', '(unknown)'), drive_file_id=file_meta.get('id', ''),
                outcome='failed', detail=f'unexpected error: {e}'))

    result.finished_at = datetime.datetime.now(datetime.timezone.utc)
    logger.info('Pipeline run complete. %d file(s) failed, %d unexpected error(s).',
                len(result.files_failed), result.unexpected_errors)
    return result


def main() -> int:
    """Thin cron entrypoint: run the pipeline, print a human-readable
    summary (matching today's log output style, since Railway's cron logs
    are how the household currently verifies runs), exit with the right
    code."""
    logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(message)s')
    result = run_pipeline()
    for line in result.summary_lines():
        print(line, flush=True)
    return 0 if result.ok else 1


if __name__ == '__main__':
    sys.exit(main())
