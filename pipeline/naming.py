"""Standardized filename builder, matching the convention already used in
the "Source Documents (Standardized Names)" Drive folder:

    "2026-05 - AMEX ConnectMiles (...4473) statement.csv"
    "2026-07..2026-08 - Huntington Checking (...3491) statement.pdf"

A single-month statement gets a "YYYY-MM" prefix; a statement whose parsed
transactions span more than one calendar month gets a "YYYY-MM..YYYY-MM"
date-range prefix. A file that covers more than one account (Capital One
360 Checking+Savings from one PDF; Panama Mastercard primary+supplementary
from one CSV) joins the account names with " & " -- there was no existing
example of this specific case to match against (see README assumptions),
so this is a reasonable, readable extrapolation of the convention.
"""
from __future__ import annotations

import datetime


def _month_prefix(start: datetime.date, end: datetime.date) -> str:
    if (start.year, start.month) == (end.year, end.month):
        return f'{start.year:04d}-{start.month:02d}'
    return f'{start.year:04d}-{start.month:02d}..{end.year:04d}-{end.month:02d}'


def build_standardized_filename(*, start: datetime.date, end: datetime.date,
                                 account_names: list, extension: str) -> str:
    prefix = _month_prefix(start, end)
    names = ' & '.join(account_names)
    ext = extension if extension.startswith('.') else f'.{extension}'
    return f'{prefix} - {names} statement{ext}'


def extension_of(filename: str) -> str:
    if '.' not in filename:
        return ''
    return '.' + filename.rsplit('.', 1)[-1].lower()
