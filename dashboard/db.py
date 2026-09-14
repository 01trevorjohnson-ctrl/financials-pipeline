"""Supabase access for the dashboard service.

IMPORTANT DEVIATION FROM THE LITERAL SPEC, DOCUMENTED HERE AND IN README.md:
public.* tables have RLS policies requiring `auth.role() = 'authenticated'`.
A bare SUPABASE_ANON_KEY request (no signed-in Supabase Auth session) has
role 'anon', not 'authenticated' -- so it would be silently blocked by RLS
and every query would come back empty, regardless of DASHBOARD_PASSWORD.
Standing up real per-user Supabase Auth sign-in is exactly the "full auth"
the task brief says to skip for v1 ("gate the whole dashboard behind a
single shared password ... rather than building full auth").

So: this module prefers SUPABASE_SERVICE_KEY (server-side only, never sent
to the browser) when it's set, which bypasses RLS entirely and just works
with the schema as-is. It falls back to SUPABASE_ANON_KEY only if no
service key is configured -- which will only successfully read/write once
the household later sets up real Supabase Auth (a dedicated signed-in user)
or relaxes the RLS policies; until then it will return empty results. The
whole app is already gated by DASHBOARD_PASSWORD before any Supabase call
is made (see main.py), so using the service key here does not weaken the
"only two household members can view this" property -- it's the same trust
model as any server-side app holding a backend DB credential behind a login.

All queries here go through supabase-py's ``.table(...).select(...)``
builder (PostgREST), same pattern as pipeline/db.py -- no raw SQL anywhere
in this app, including the Trends and Ask pages (see main.py for how their
month-bucketing/aggregation is done in Python once the filtered rows are
fetched).
"""
from __future__ import annotations

import calendar
import datetime
import os
from collections import defaultdict
from functools import lru_cache

from supabase import create_client, Client


PAGE_SIZE = 1000
MAX_PAGES = 100  # safety valve (100k rows) against a runaway loop; not a real ceiling


def _fetch_all(build_query) -> list:
    """`build_query` is a zero-arg callable that returns a FRESH,
    not-yet-ranged supabase-py table builder each time it's called (e.g.
    ``lambda: client.table('transactions').select('date').eq('flow', 'Spend')``)
    -- NOT an already-built query object. This function calls it once per
    page and applies `.range()` to that fresh instance.

    That "fresh instance per call" requirement is not stylistic: postgrest-py's
    `.range()` adds to httpx's `QueryParams`, which is immutable per call --
    calling `.range()` a second time on the SAME builder instance appends a
    second offset/limit pair (`offset=0&offset=1000&...`) instead of replacing
    the first, silently corrupting pagination. Rebuilding the query fresh for
    every page sidesteps that entirely.

    PostgREST (what supabase-py talks to) caps a single response at a
    server-configured default -- 1000 rows on a stock Supabase project --
    with NO error or warning when a query has more matches than that; it
    just silently returns the first page. Every `transactions` query in this
    app must go through this helper instead of a bare `.execute()`, because
    the household already has 2000+ rows: any unpaginated query here quietly
    undercounts real data instead of failing loudly, which is worse than a
    crash. (`needs_review` queries are filtered to `status='open'`, a small
    set, and are left as plain `.execute()` calls.)"""
    rows: list = []
    start = 0
    for _ in range(MAX_PAGES):
        resp = build_query().range(start, start + PAGE_SIZE - 1).execute()
        batch = resp.data or []
        rows.extend(batch)
        if len(batch) < PAGE_SIZE:
            break
        start += PAGE_SIZE
    return rows


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_ANON_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL and (SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY) must be set in the '
            'environment. See README.md "Environment variables".')
    return create_client(url, key)


# ---------------------------------------------------------------------------
# Home: pipeline status summary
# ---------------------------------------------------------------------------
def get_open_needs_review_count(client: Client) -> int:
    resp = client.table('needs_review').select('id', count='exact').eq('status', 'open').execute()
    return resp.count or 0


def get_last_pipeline_activity(client: Client) -> dict | None:
    """Most recent processed_statements row (by processed_at), used as a
    proxy for "when did the pipeline last do something" on the Home page.

    Caveat, worth knowing: processed_at is set once at insert time and is
    NOT bumped by pipeline/db.py's update_processed_statement() on a retry
    of a previously-errored file, and a pipeline run that finds zero new
    Drive files touches this table at all -- so this reflects the last time
    a *file* was processed, not strictly the last cron invocation. There is
    no separate "pipeline run log" table (this repo intentionally does not
    alter the Supabase schema -- see README.md), so this is the closest
    honest signal available without adding one.
    """
    resp = (client.table('processed_statements')
            .select('original_filename, standardized_filename, status, reconciliation_ok, '
                    'reconciliation_detail, row_count, processed_at')
            .order('processed_at', desc=True).limit(1).execute())
    rows = resp.data or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Needs Review
# ---------------------------------------------------------------------------
def get_open_needs_review(client: Client) -> list:
    resp = (client.table('needs_review').select('*')
            .eq('status', 'open').order('created_at', desc=True).execute())
    return resp.data or []


def get_active_categories(client: Client) -> list:
    resp = client.table('category_keys').select('category').eq('active', True).execute()
    return sorted({r['category'] for r in (resp.data or [])})


# ---------------------------------------------------------------------------
# Home: statement coverage checklist (see dashboard/coverage.py)
# ---------------------------------------------------------------------------
def get_processed_account_names_and_periods(client: Client) -> list:
    """account_name/statement_period for every successfully-processed
    statement -- the raw material dashboard/coverage.py matches against the
    13 known account names to find each one's most recent statement."""
    return _fetch_all(lambda: client.table('processed_statements')
                       .select('account_name, statement_period').eq('status', 'processed'))


def resolve_needs_review(client: Client, review_id: str, category: str) -> None:
    review_resp = client.table('needs_review').select('*').eq('id', review_id).limit(1).execute()
    review_rows = review_resp.data or []
    if not review_rows:
        return
    review = review_rows[0]

    # Look up the flow that goes with this category from category_keys, so
    # we don't leave a stale/mismatched flow on the transaction.
    flow = None
    ck_resp = client.table('category_keys').select('flow').eq('category', category).limit(1).execute()
    if ck_resp.data:
        flow = ck_resp.data[0]['flow']

    if review.get('transaction_id'):
        update = {'category': category}
        if flow:
            update['flow'] = flow
        client.table('transactions').update(update).eq('id', review['transaction_id']).execute()

    client.table('needs_review').update({
        'status': 'resolved',
        'resolved_category': category,
        'resolved_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
    }).eq('id', review_id).execute()


# ---------------------------------------------------------------------------
# Trends: month x category spend, via the PostgREST table builder + Python
# bucketing (date_trunc('month', ...) grouping isn't expressible through
# supabase-py's builder, so raw filtered rows are pulled and bucketed here --
# same approach used everywhere else in this file, no second raw-SQL path).
# ---------------------------------------------------------------------------
def get_distinct_spend_categories(client: Client) -> list:
    """Distinct transactions.category values actually present for
    flow='Spend' -- real data, not the category_keys catalog (which may
    include categories with zero transactions, or exclude ad hoc ones)."""
    rows = _fetch_all(lambda: client.table('transactions').select('category').eq('flow', 'Spend'))
    return sorted({r['category'] for r in rows if r.get('category')})


def get_monthly_spend_by_category(client: Client) -> dict:
    """Returns {category: {'YYYY-MM': total_amount, ...}, ...} for every
    flow='Spend' transaction, bucketed by calendar month in Python."""
    rows = _fetch_all(lambda: client.table('transactions').select('date, category, amount').eq('flow', 'Spend'))

    by_cat: dict = defaultdict(lambda: defaultdict(float))
    for r in rows:
        cat = r.get('category') or 'Uncategorized'
        date_str = r.get('date')
        amt = r.get('amount') or 0
        if not date_str:
            continue
        month_key = date_str[:7]  # 'YYYY-MM-DD' -> 'YYYY-MM'
        by_cat[cat][month_key] += amt

    return {cat: dict(months) for cat, months in by_cat.items()}


def top_categories_by_total_spend(monthly_by_category: dict, n: int = 6) -> list:
    totals = [(cat, sum(months.values())) for cat, months in monthly_by_category.items()]
    totals.sort(key=lambda kv: -kv[1])
    return [cat for cat, _total in totals[:n]]


def month_range(monthly_by_category: dict) -> list:
    """Sorted list of every 'YYYY-MM' key present across all categories, so
    every series can be plotted against the same x-axis (missing months ->
    0)."""
    months = set()
    for months_dict in monthly_by_category.values():
        months.update(months_dict.keys())
    return sorted(months)


def month_label(month_key: str) -> str:
    year, month = month_key.split('-')
    return f'{calendar.month_abbr[int(month)]} {year}'


# ---------------------------------------------------------------------------
# Ask: LLM-assisted Q&A, backed by a validated structured filter spec (see
# main.py ask_submit()) run through this same table builder -- never raw SQL,
# never string-interpolating LLM output into a query.
# ---------------------------------------------------------------------------
def get_distinct_categories_all_flows(client: Client) -> list:
    rows = _fetch_all(lambda: client.table('transactions').select('category'))
    return sorted({r['category'] for r in rows if r.get('category')})


def get_distinct_flows(client: Client) -> list:
    rows = _fetch_all(lambda: client.table('transactions').select('flow'))
    return sorted({r['flow'] for r in rows if r.get('flow')})


def run_ask_query(client: Client, *, date_from: str | None, date_to: str | None,
                   categories: list | None, flow: str | None) -> list:
    """Runs a filtered, parameterized query via the supabase-py table
    builder using an ALREADY-VALIDATED spec (validation happens in
    main.py's ask_submit() against the real category/flow values -- this
    function does not trust its caller further, but it also does no
    string interpolation of any kind, so there is no injection surface
    regardless)."""
    def build():
        q = client.table('transactions').select('date, amount, category, flow, merchant, cardholder')
        if date_from:
            q = q.gte('date', date_from)
        if date_to:
            q = q.lte('date', date_to)
        if categories:
            q = q.in_('category', categories)
        if flow:
            q = q.eq('flow', flow)
        return q

    return _fetch_all(build)


# ---------------------------------------------------------------------------
# Export: the household's own copy of the ledger, in formats meant to be
# taken elsewhere -- a spreadsheet tool, or pasted/uploaded to an LLM for
# further analysis -- now that the source-of-truth Excel workbook is
# retired in favor of Supabase. Every query here goes through _fetch_all
# for the same reason as everywhere else in this file: an unpaginated
# query silently truncates at 1000 rows.
# ---------------------------------------------------------------------------
def get_all_transactions_for_export(client: Client) -> list:
    rows = _fetch_all(lambda: client.table('transactions')
                       .select('date, time, cardholder, amount, points, balance, status, type, '
                               'merchant, description, card, flow, category'))
    return sorted(rows, key=lambda r: r.get('date') or '')


def get_full_export_bundle(client: Client) -> dict:
    """Every table this app reads from, each as a plain list of dicts --
    meant for a single "download everything" JSON export. Table names are
    the dict keys so the file is self-describing without a README."""
    return {
        'accounts': _fetch_all(lambda: client.table('accounts').select('*')),
        'category_keys': _fetch_all(lambda: client.table('category_keys').select('*')),
        'processed_statements': _fetch_all(lambda: client.table('processed_statements').select('*')),
        'transactions': get_all_transactions_for_export(client),
        'needs_review': _fetch_all(lambda: client.table('needs_review').select('*')),
        'assets_liabilities': _fetch_all(lambda: client.table('assets_liabilities').select('*')),
        'income_paychecks': _fetch_all(lambda: client.table('income_paychecks').select('*')),
    }
