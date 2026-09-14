"""Categorization: category_keys keyword matching + the special cases
encoded in category_keys.notes that aren't expressible as plain keyword
substrings, plus the fallback policy for genuinely unrecognized merchants.

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

import re

UNCATEGORIZED = 'Uncategorized'
NEEDS_REVIEW_THRESHOLD = 50.00  # dollars; adjustable -- see README

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
