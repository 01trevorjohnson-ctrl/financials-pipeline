"""Backend for the "Ask" page: plain-English Q&A over the household's
transactions, backed by the Claude API.

Two Claude calls, exactly as scoped in the brief -- the LLM NEVER generates
or executes raw SQL against the service-role-authenticated Supabase
connection (SUPABASE_SERVICE_KEY bypasses RLS entirely, so LLM-generated
SQL would be a real injection/safety risk):

  1. ``build_filter_spec()`` -- gives Claude the question, the available
     fields, and the REAL distinct category/flow values (queried from
     Supabase, not hardcoded) and asks for a small structured JSON filter
     spec, guaranteed-valid JSON via ``output_config.format`` (a JSON
     Schema). ``validate_spec()`` then whitelists every field server-side
     (categories/flow against the real queried values, dates parsed and
     range-checked) before it's used for anything -- the raw model output
     is never trusted past this point.
  2. The validated spec is run through ``dashboard/db.py``'s existing
     supabase-py table builder (``run_ask_query`` + ``compute_metric``
     below) -- same pattern as every other query in this app, never a
     string-interpolated query of any kind.
  3. ``phrase_answer()`` -- a second Claude call, given the original
     question plus the actual computed numbers, asked to phrase a short
     natural-language answer. The numbers themselves are always shown
     alongside it (see templates/ask.html) so nobody has to trust an opaque
     LLM sentence with no backing data.

Model choice: a small/fast model (``claude-haiku-4-5``), per this
project's brief ("a small/fast model is fine for both calls given the
queries are simple") -- both calls here are a short JSON extraction and a
one-paragraph phrasing, not open-ended reasoning.
"""
from __future__ import annotations

import datetime
import json
import os
from collections import defaultdict

from . import db

ASK_MODEL = 'claude-haiku-4-5'

FILTER_SPEC_SCHEMA = {
    'type': 'object',
    'properties': {
        'metric': {
            'type': 'string',
            'enum': ['sum', 'count', 'average', 'list'],
            'description': (
                'What to compute over the matching transactions. Use "list" -- not sum/count/'
                'average -- for any question asking about specific transaction(s) rather than a '
                'total: "what was our most recent transaction", "show me our Amazon purchases in '
                'June", "what did we buy at Costco last week". sum/count/average only answer '
                '"how much"/"how many"/"on average" questions; they cannot identify which '
                'transaction(s) something was.'
            ),
        },
        'limit': {
            'type': 'integer',
            'description': (
                'Only used when metric="list": how many matching transactions to return, most '
                'recent first. Use 1 for "the most recent transaction" / "the last time we...", '
                'a small number (5-20) for "show me our recent X purchases", ignored otherwise.'
            ),
        },
        'date_from': {
            'type': 'string',
            'description': 'Inclusive start date as YYYY-MM-DD, or "" for no lower bound.',
        },
        'date_to': {
            'type': 'string',
            'description': 'Inclusive end date as YYYY-MM-DD, or "" for no upper bound.',
        },
        'categories': {
            'type': 'array',
            'items': {'type': 'string'},
            'description': 'Exact category values to filter to, or [] for every category.',
        },
        'flow': {
            'type': 'string',
            'description': 'An exact flow value to filter to, or "" for every flow.',
        },
        'group_by': {
            'type': 'string',
            'enum': ['category', 'month', 'none'],
            'description': 'How to break down the result, or "none" for a single total. Ignored when metric="list".',
        },
    },
    'required': ['metric', 'limit', 'date_from', 'date_to', 'categories', 'flow', 'group_by'],
    'additionalProperties': False,
}

LIST_LIMIT_DEFAULT = 10
LIST_LIMIT_MAX = 50


def is_configured() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def _client():
    import anthropic
    return anthropic.Anthropic()


def _safe_date(s) -> str | None:
    if not s:
        return None
    try:
        datetime.date.fromisoformat(s)
        return s
    except (ValueError, TypeError):
        return None


def validate_spec(raw: dict, categories: list, flows: list) -> dict:
    """Whitelist every field of the model's raw filter spec against the
    REAL distinct values queried from Supabase, and sanity-check the dates.
    Never trusts the model's output past this point."""
    metric = raw.get('metric')
    if metric not in ('sum', 'count', 'average', 'list'):
        metric = 'sum'

    limit = raw.get('limit')
    if not isinstance(limit, int) or limit < 1:
        limit = LIST_LIMIT_DEFAULT
    limit = min(limit, LIST_LIMIT_MAX)

    date_from = _safe_date(raw.get('date_from'))
    date_to = _safe_date(raw.get('date_to'))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    raw_categories = raw.get('categories') or []
    if not isinstance(raw_categories, list):
        raw_categories = []
    cats = [c for c in raw_categories if isinstance(c, str) and c in categories]
    cats = cats or None

    flow = raw.get('flow')
    flow = flow if flow in flows else None

    group_by = raw.get('group_by')
    if group_by not in ('category', 'month'):
        group_by = None
    if metric == 'list':
        group_by = None  # list returns individual rows, grouping doesn't apply

    return {'metric': metric, 'limit': limit, 'date_from': date_from, 'date_to': date_to,
            'categories': cats, 'flow': flow, 'group_by': group_by}


def build_filter_spec(question: str, categories: list, flows: list) -> dict:
    today = datetime.date.today().isoformat()
    system = (
        'You translate a household\'s plain-English question about their finances into a '
        'small structured filter spec over their `transactions` table. Available fields: '
        'date (YYYY-MM-DD), amount (positive = money out/spend, negative = money in), flow, '
        f'category, merchant, cardholder. Today\'s date is {today}. '
        f'The ONLY valid category values are: {", ".join(categories)}. '
        f'The ONLY valid flow values are: {", ".join(flows)}. '
        'Use "" for date_from/date_to/flow and [] for categories when the question does not '
        'imply a value for that field. "spend"/"spent"/"spending" in a question means '
        'flow="Spend". Infer relative date phrases ("this month", "last month", "in July", '
        '"this year", "last 90 days") relative to today\'s date. If the question asks "what\'s '
        'our biggest category", use metric="sum" and group_by="category" with no category '
        'filter, so every category can be compared. If the question asks about a specific '
        'transaction or a handful of them by name/place/recency ("most recent", "last time we '
        'bought from X", "show me our Y purchases") rather than a total, use metric="list" with '
        'an appropriate limit -- these questions cannot be answered by a sum/count/average, only '
        'by looking at the actual matching rows.'
    )
    resp = _client().messages.create(
        model=ASK_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{'role': 'user', 'content': question}],
        output_config={'format': {'type': 'json_schema', 'schema': FILTER_SPEC_SCHEMA}},
    )
    text = next(b.text for b in resp.content if b.type == 'text')
    return json.loads(text)


def compute_metric(rows: list, metric: str, group_by: str | None, limit: int = LIST_LIMIT_DEFAULT) -> dict:
    if metric == 'list':
        # Most-recent-first covers "most recent transaction" directly, and
        # is the most useful default ordering for "show me our X purchases"
        # too. transaction_count is the TOTAL matching count (before the
        # limit is applied), so the answer can say e.g. "here are the 10
        # most recent of 47 matching transactions".
        ordered = sorted(rows, key=lambda r: r.get('date') or '', reverse=True)
        picked = ordered[:limit]
        return {
            'transactions': [
                {
                    'date': r.get('date'),
                    'merchant': r.get('merchant'),
                    'amount': round(r.get('amount') or 0, 2),
                    'category': r.get('category'),
                    'cardholder': r.get('cardholder'),
                }
                for r in picked
            ],
            'transaction_count': len(rows),
        }

    def agg(values: list) -> float:
        if metric == 'count':
            return len(values)
        if metric == 'average':
            return (sum(values) / len(values)) if values else 0.0
        return sum(values)  # 'sum'

    if group_by is None:
        values = [r.get('amount') or 0 for r in rows]
        return {'total': round(agg(values), 2), 'transaction_count': len(rows)}

    buckets: dict = defaultdict(list)
    if group_by == 'category':
        for r in rows:
            buckets[r.get('category') or 'Uncategorized'].append(r.get('amount') or 0)
    else:  # 'month'
        for r in rows:
            date_str = r.get('date') or ''
            buckets[date_str[:7] or 'unknown'].append(r.get('amount') or 0)

    breakdown = {key: round(agg(vals), 2) for key, vals in buckets.items()}
    # Sort by magnitude, descending, for a more useful default display.
    breakdown = dict(sorted(breakdown.items(), key=lambda kv: -abs(kv[1])))
    return {'breakdown': breakdown, 'transaction_count': len(rows)}


def phrase_answer(question: str, result_data: dict) -> str:
    system = (
        'You answer a household\'s finance question using ONLY the exact data given below, '
        'already computed/looked up from their real transaction data -- never invent or adjust a '
        'number, date, or merchant name. Give a short, direct answer (one to three sentences), '
        'formatting dollar amounts like $1,234.56. If the data is a list of individual '
        'transactions (not a total/count/average), name the specific one(s) that answer the '
        'question -- e.g. for "most recent transaction", name the single most recent row\'s date, '
        'merchant, and amount, not just how many rows were returned.'
    )
    resp = _client().messages.create(
        model=ASK_MODEL,
        max_tokens=512,
        system=system,
        messages=[{
            'role': 'user',
            'content': f'Question: {question}\n\nComputed data: {json.dumps(result_data)}',
        }],
    )
    return next((b.text for b in resp.content if b.type == 'text'), '').strip()


def answer_question(supabase_client, question: str) -> dict:
    """Runs the full two-call flow and returns
    {'answer': str, 'spec': dict, 'data': dict}. Raises on API/network
    failure -- the caller (main.py) is responsible for catching that and
    showing a friendly error."""
    categories = db.get_distinct_categories_all_flows(supabase_client)
    flows = db.get_distinct_flows(supabase_client)

    raw_spec = build_filter_spec(question, categories, flows)
    spec = validate_spec(raw_spec, categories, flows)

    rows = db.run_ask_query(supabase_client, date_from=spec['date_from'], date_to=spec['date_to'],
                             categories=spec['categories'], flow=spec['flow'])
    result_data = compute_metric(rows, spec['metric'], spec['group_by'], spec['limit'])

    answer_text = phrase_answer(question, result_data)
    return {'answer': answer_text, 'spec': spec, 'data': result_data}
