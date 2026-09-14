"""Categorization: category_keys keyword matching + the special cases
encoded in category_keys.notes that aren't expressible as plain keyword
substrings, plus an LLM fallback (see llm_categorize_uncategorized below)
for rows no keyword matches, plus the final fallback policy (plain
Uncategorized + needs_review) for whatever's left after that.

Special cases implemented here (see category_keys.notes for the household's
own wording of each):

1. Self-transfer vs. gift: a Zelle/ACH transfer where the counterparty IS
   Trevor Johnson is a self-transfer (Account transfer / Transfer), not a
   gift -- even though it might otherwise match a Gifts-category keyword
   like "TEF A :". This must run BEFORE the generic keyword loop.

2. Wise transfers to Colombia: NOT split. Each whole Wise row is assigned
   alternately ~half Charitable giving / ~half Investment (Navarra real
   estate), ordered by date. Alternation state is derived from what's
   already in the database (counts of existing Wise-tagged rows) so it
   stays balanced across separate pipeline runs, not just within one
   statement's batch.

3. Income vs. Refund precedence for "ACH CRE ...": handled implicitly by
   priority ordering (Income's "NOVARTIS" keyword is checked at a lower
   priority number than Refund's generic "ACH CRE" keyword), so no special
   code is needed beyond respecting category_keys.priority order.

4. PedidosYa dual-row-per-order: no special code needed -- both the order
   row and the tip row contain "PEDIDOSYA" and both correctly match the
   Delivery category via the normal keyword loop.

5. TOTAL ITBMS single statement-level row: produced by the parser as a
   single row (see parsers/panama_mastercard_csv.py and
   parsers/amex_connectmiles_csv.py); it matches the Insurance & fees
   category via its own "TOTAL ITBMS" keyword through the normal loop.
"""
from __future__ import annotations

import json
import logging
import os
import re

UNCATEGORIZED = 'Uncategorized'
NEEDS_REVIEW_THRESHOLD = 50.00  # dollars; adjustable -- see README

LLM_MODEL = 'claude-haiku-4-5'  # small/fast classification call, same choice as dashboard/ask.py
LLM_MAX_ROWS_PER_CALL = 200  # safety valve; a real statement never gets close to this

logger = logging.getLogger(__name__)

_ZELLE_TREVOR_RE = re.compile(r'ACH\s+\S*\s*TREVOR JOHNSON', re.I)


def _match_text(merchant: str, description: str) -> str:
    return f'{merchant or ""} | {description or ""}'.upper()


def is_self_transfer_to_trevor(description: str) -> bool:
    d = (description or '').upper()
    if 'ZELLE' in d and ('TREVOR JOHNSON' in d or 'TREVORJOHNSON' in d):
        return True
    if _ZELLE_TREVOR_RE.search(description or ''):
        return True
    return False


def is_wise_transfer(merchant: str, description: str) -> bool:
    return 'WISE' in _match_text(merchant, description)


def fallback_flow(txn_type: str, amount) -> str:
    """Best-effort Flow guess for a transaction that matched no
    category_keys row and isn't a special case. Mirrors the flow-detection
    logic already present in the reference parsers (build_xlsx.py
    classify_flow): payments settle a card (CC payment), interest earned is
    its own flow, a Credit-type or negative amount reads as money coming
    back in (Refund), everything else defaults to ordinary Spend.
    """
    t = (txn_type or '').strip()
    if t == 'Payment':
        return 'CC payment'
    if t == 'Interest':
        return 'Interest earned'
    if t == 'Credit' or (amount is not None and amount < 0):
        return 'Refund'
    return 'Spend'


def is_llm_configured() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def _llm_client():
    import anthropic
    return anthropic.Anthropic()


def llm_categorize_uncategorized(rows_with_indices: list, category_flow_map: dict) -> dict:
    """One batched Claude call classifying every keyword-unmatched row from
    ONE statement at once (a "row" here is (original_index, TxnRow-like)).
    ``category_flow_map`` is {category: flow}, built by the caller from the
    same category_keys rows already loaded for keyword matching -- so the
    model is constrained (via JSON-schema enum) to categories the household
    actually uses, never a category it invents.

    Returns {original_index: (category, flow)} -- ONLY for rows the model
    confidently placed in a real category. Rows it left/returned as
    "Uncategorized", rows with an invalid index/category, and rows dropped
    by a parse failure are simply absent from the result; the caller's
    existing (Uncategorized, fallback_flow(...)) result for those rows is
    untouched. This function never raises: a missing API key, a network
    error, a malformed response, or anything else means "no help this
    time," not a failed pipeline run -- keyword categorization plus the
    existing needs_review threshold is the safety net this degrades to.
    """
    if not rows_with_indices or not is_llm_configured():
        return {}
    if len(rows_with_indices) > LLM_MAX_ROWS_PER_CALL:
        logger.warning('Skipping LLM categorization: %d uncategorized rows exceeds the %d-row '
                        'safety cap for one call.', len(rows_with_indices), LLM_MAX_ROWS_PER_CALL)
        return {}

    categories = sorted(category_flow_map.keys())
    schema = {
        'type': 'object',
        'properties': {
            'results': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'index': {'type': 'integer'},
                        'category': {'type': 'string', 'enum': categories + [UNCATEGORIZED]},
                    },
                    'required': ['index', 'category'],
                    'additionalProperties': False,
                },
            },
        },
        'required': ['results'],
        'additionalProperties': False,
    }
    lines = [
        f'{idx}: merchant="{row.merchant or ""}" description="{row.description or ""}" '
        f'amount={row.amount} type="{row.type or ""}"'
        for idx, row in rows_with_indices
    ]
    system = (
        'You categorize household credit-card/bank transactions for a US/Panama household, for '
        'rows that did not match any of their existing keyword rules. Assign each transaction '
        f'EXACTLY ONE of these categories: {", ".join(categories)}. Use "{UNCATEGORIZED}" ONLY '
        'when you genuinely cannot tell -- an unfamiliar or foreign-language merchant name is not '
        'by itself a reason to give up; use the merchant name, description, amount, and typical '
        'purchase patterns to make a reasonable call. Many merchant names are in Spanish or are '
        'Panama-local businesses (restaurants, pharmacies, transport, utilities). Return exactly '
        'one result per transaction index given, covering every index exactly once.'
    )
    try:
        resp = _llm_client().messages.create(
            model=LLM_MODEL,
            max_tokens=4096,
            system=system,
            messages=[{'role': 'user', 'content': '\n'.join(lines)}],
            output_config={'format': {'type': 'json_schema', 'schema': schema}},
        )
        text = next(b.text for b in resp.content if b.type == 'text')
        parsed = json.loads(text)
    except Exception as e:
        logger.warning('LLM categorization call failed, falling back to Uncategorized: %s', e)
        return {}

    valid_indices = {idx for idx, _row in rows_with_indices}
    out = {}
    for item in parsed.get('results') or []:
        idx = item.get('index')
        category = item.get('category')
        if idx not in valid_indices or category not in category_flow_map:
            continue  # covers category == UNCATEGORIZED too -- nothing to override
        out[idx] = (category, category_flow_map[category])
    return out


class Categorizer:
    """Stateful categorizer for one pipeline run.

    ``category_keys`` is the list of rows from public.category_keys
    (active only), already sorted ascending by priority.
    ``wise_giving_count``/``wise_invest_count`` seed the Wise alternation
    from what's already in the database so a fresh pipeline run continues
    the alternation instead of restarting it.
    """

    def __init__(self, category_keys, wise_giving_count: int = 0, wise_invest_count: int = 0):
        self.category_keys = category_keys
        self._wise_giving = wise_giving_count
        self._wise_invest = wise_invest_count

    def _next_wise_category(self) -> str:
        # Keep the two buckets as balanced as possible; ties favor Giving,
        # matching the household's original i%2==0 -> giving convention.
        if self._wise_giving <= self._wise_invest:
            self._wise_giving += 1
            return 'Charitable giving'
        self._wise_invest += 1
        return 'Investment'

    def categorize_row(self, merchant: str, description: str, txn_type: str, amount):
        """Return (category, flow) for one transaction."""
        if is_self_transfer_to_trevor(description):
            return 'Account transfer', 'Transfer'

        if is_wise_transfer(merchant, description):
            category = self._next_wise_category()
            flow = category  # 'Charitable giving' or 'Investment' are both valid flow values
            return category, flow

        text = _match_text(merchant, description)
        for row in self.category_keys:
            keywords = row.get('keywords') or []
            if any(kw in text for kw in keywords):
                return row['category'], row['flow']

        return UNCATEGORIZED, fallback_flow(txn_type, amount)

    def categorize_batch(self, txn_rows):
        """Categorize a list of parsers.common.TxnRow-like objects (must
        have .merchant, .description, .type, .amount, .date). Wise rows
        within the SAME batch are alternated in date order before the
        (still database-seeded) counters are consulted, so a single
        statement with multiple Wise rows alternates correctly internally
        too.
        """
        results = [None] * len(txn_rows)
        wise_indices = sorted(
            (i for i, r in enumerate(txn_rows) if is_wise_transfer(r.merchant, r.description)),
            key=lambda i: txn_rows[i].date,
        )
        non_wise_indices = [i for i in range(len(txn_rows)) if i not in set(wise_indices)]

        for i in non_wise_indices:
            r = txn_rows[i]
            results[i] = self.categorize_row(r.merchant, r.description, r.type, r.amount)
        for i in wise_indices:
            r = txn_rows[i]
            if is_self_transfer_to_trevor(r.description):
                results[i] = ('Account transfer', 'Transfer')
            else:
                category = self._next_wise_category()
                results[i] = (category, category)
        return results
