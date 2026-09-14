"""Color system for the dashboard, matching the FAMILY app's exact visual
language (see ``family-finance/src/app/_shared/colors.ts`` and
``family-finance/src/app/budget/category-config.tsx`` -- read directly,
not just summarized, while building this).

- ``FINANCES_COLOR`` is the FAMILY app's own accent for its link-out tile to
  this dashboard (``FINANCES_COLOR`` in colors.ts) -- reused verbatim as
  this app's primary accent so the two apps read as one system.
- ``SAVINGS_COLOR`` is reused verbatim too (gold/savings accent), so this
  app never picks a category color that clashes with it.
- ``CATEGORY_COLORS`` reuses the FAMILY app's exact category hex values for
  every category name that corresponds 1:1 between the two apps (e.g.
  "Groceries" is #3FA672 in both). For categories that exist only in this
  app's data (public.category_keys / transactions.category), new colors
  were hand-picked to read well on both the light and dark ``--bud-glass``
  backgrounds and to stay visually distinct from FINANCES_COLOR,
  SAVINGS_COLOR, and every FAMILY category color.
"""
from __future__ import annotations

import colorsys
import hashlib

# ---------------------------------------------------------------------------
# Reused verbatim from family-finance/src/app/_shared/colors.ts
# ---------------------------------------------------------------------------
FINANCES_COLOR = '#5568D8'
SAVINGS_COLOR = '#D4A72C'

# ---------------------------------------------------------------------------
# Category colors. Keys are public.category_keys.category / transactions.category
# values exactly as they exist in Supabase (queried, not guessed -- see
# dashboard/db.py get_distinct_categories()).
# ---------------------------------------------------------------------------
CATEGORY_COLORS = {
    # ---- reused verbatim: these category names correspond 1:1 with a
    # FAMILY app category of the same real-world meaning, so we reuse
    # FAMILY's exact hex (family-finance/src/app/budget/category-config.tsx)
    'Groceries': '#3FA672',
    'Health, medical & fitness': '#2BB6A3',       # FAMILY "Healthcare"
    'Dining & entertainment': '#E8734A',           # FAMILY "Dining"
    'Delivery': '#D9A441',
    'Amazon & shipping': '#6C63C7',                # FAMILY "Amazon"
    'Shopping & personal care': '#D6608F',         # FAMILY "Clothing & Personal Care"
    'Transportation': '#3E8FD1',
    'Home & household': '#B0703C',                 # FAMILY "Home"

    # ---- FP-only categories: new colors, chosen to be distinct from the
    # eight reused above, from FINANCES_COLOR/SAVINGS_COLOR, and from each
    # other; mid-tone (readable on both light and dark --bud-glass panels).
    'Rent': '#C0475C',
    'Travel': '#3AA7C9',
    'Student loans': '#8A9A3E',
    'Childcare & education': '#7B5FCF',
    'Gifts & support to individuals': '#C0569C',
    'Utilities & internet': '#A68A4F',
    'Taxes & professional': '#5C7A8C',
    'Cash': '#8C7A6E',
    'Subscriptions & software': '#6F8FBF',
    'Insurance & fees': '#9C6B4F',
    'Charitable giving': '#4F9E7A',
    'Uncategorized': '#8A8A8A',

    # ---- non-Spend flows/categories, included for completeness in case
    # any view ever lists them alongside spend categories.
    'Account transfer': '#6E7A99',
    'Credit-card payment': '#8B8FA6',
    'Income': '#4FA37A',
    'Investment & interest': '#7A8FC7',
    'Refund': '#5FAFA0',
}


def _fallback_color(name: str) -> str:
    """Deterministic color for any category not in the table above (e.g. a
    brand-new category_keys row added after this file was written), picked
    from HSL space at fixed saturation/lightness so it stays readable on
    both light and dark glass panels without needing to hand-pick it."""
    digest = hashlib.sha256((name or '').encode('utf-8')).hexdigest()
    hue = (int(digest[:8], 16) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.5, 0.42)
    return '#{:02x}{:02x}{:02x}'.format(round(r * 255), round(g * 255), round(b * 255))


def get_category_color(name: str) -> str:
    if name in CATEGORY_COLORS:
        return CATEGORY_COLORS[name]
    return _fallback_color(name)
