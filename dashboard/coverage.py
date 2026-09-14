"""Statement coverage: does each known account have a recent statement on
file? Powers the "Statements needed" checklist on the Home page.

The overdue/not-overdue decision (get_account_coverage) is entirely
deterministic -- date arithmetic against processed_statements, no LLM
involved -- because that decision has to be right regardless of whether an
LLM call is configured or available. llm_headline() is a separate, optional
layer on top that only PHRASES a short one-line summary from the
already-computed coverage list (same compute-in-Python-then-let-the-LLM-
phrase-it split as dashboard/ask.py), and falls back to a plain
Python-built sentence on a missing ANTHROPIC_API_KEY or any API failure --
the checklist itself, and its correctness, never depends on the LLM being
up.
"""
from __future__ import annotations

import datetime
import os

from pipeline import accounts as account_names

from . import db

# ~1 statement cycle (30 days) plus a couple weeks' buffer for the
# statement to be generated/downloaded/uploaded. Every account in this
# household's data is on a roughly-monthly cycle (confirmed against real
# processed_statements history -- cycle *day* varies by account, e.g. Citi
# Costco closes near the 1st-3rd rather than month-end, but the ~30-day
# cadence itself doesn't), so one threshold works for all 13 accounts
# without needing a per-account cycle-length field the schema doesn't have.
STALE_THRESHOLD_DAYS = 45

LLM_MODEL = 'claude-haiku-4-5'


def get_account_coverage(client, today: datetime.date | None = None) -> list[dict]:
    """One entry per known account (pipeline.accounts.ALL): its most recent
    successfully-processed statement_period (None if it has never had one),
    how many days old that is, and whether that exceeds
    STALE_THRESHOLD_DAYS.

    Matched by substring against processed_statements.account_name because
    a statement covering more than one account (e.g. a combined Capital
    One 360 Checking+Savings PDF) is stored there as both names joined
    with " & " (see pipeline/main.py: account_name_for_statement), not as
    a list -- none of the 13 real account names are substrings of each
    other, so this is unambiguous.

    Sorted overdue-first (most-stale first within that group), so the most
    actionable items are at the top of the checklist.
    """
    today = today or datetime.date.today()
    rows = db.get_processed_account_names_and_periods(client)

    coverage = []
    for name in account_names.ALL:
        periods = [r['statement_period'] for r in rows
                   if r.get('account_name') and name in r['account_name'] and r.get('statement_period')]
        latest = max(periods) if periods else None
        if latest:
            latest_date = latest if isinstance(latest, datetime.date) else datetime.date.fromisoformat(latest)
            days_stale = (today - latest_date).days
        else:
            latest_date = None
            days_stale = None
        overdue = days_stale is None or days_stale > STALE_THRESHOLD_DAYS
        coverage.append({
            'account': name,
            'latest_period': latest_date.isoformat() if latest_date else None,
            'days_stale': days_stale,
            'overdue': overdue,
        })

    coverage.sort(key=lambda c: (not c['overdue'], -(c['days_stale'] if c['days_stale'] is not None else 10 ** 6)))
    return coverage


def is_llm_configured() -> bool:
    return bool(os.environ.get('ANTHROPIC_API_KEY'))


def _fallback_headline(coverage: list[dict]) -> str:
    overdue = [c['account'] for c in coverage if c['overdue']]
    if not overdue:
        return 'All accounts have a recent statement on file.'
    if len(overdue) == 1:
        return f'{overdue[0]} is overdue for a new statement.'
    return f'{len(overdue)} accounts are overdue for a new statement: {", ".join(overdue)}.'


def llm_headline(coverage: list[dict], today: datetime.date | None = None) -> str:
    """A short, friendly one-to-two sentence status line for the top of the
    checklist card, built from the ALREADY-COMPUTED coverage list above --
    the model is only asked to phrase it, never to decide what's overdue.
    Falls back to _fallback_headline() on a missing key, any API error, or
    an empty response; the checklist below it always shows the real
    per-account data regardless, so a bad/missing headline never hides
    information, it just makes the summary line plainer."""
    if not coverage or not is_llm_configured():
        return _fallback_headline(coverage)
    today = today or datetime.date.today()
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=8.0)
        system = (
            "You write a single short, friendly status line (one or two sentences, no greeting) "
            "for a household finance dashboard, summarizing which accounts need a new bank/card "
            "statement uploaded. Use ONLY the account names and facts given below -- never invent "
            "an account, a date, or a reason. When naming an overdue account, use its short common "
            "name (drop the trailing '(...1234)' account-number parenthetical). If nothing is "
            "overdue, say so positively in one short sentence. Never mention field names like "
            "'overdue' or 'days_stale' verbatim."
        )
        resp = client.messages.create(
            model=LLM_MODEL,
            max_tokens=200,
            system=system,
            messages=[{
                'role': 'user',
                'content': f'Today is {today.isoformat()}. Account statement status: {coverage}',
            }],
        )
        text = next((b.text for b in resp.content if b.type == 'text'), '').strip()
        return text or _fallback_headline(coverage)
    except Exception:
        return _fallback_headline(coverage)
