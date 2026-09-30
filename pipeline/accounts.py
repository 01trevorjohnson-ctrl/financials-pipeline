"""Exact ``accounts.name`` strings from Supabase (public.accounts), as seeded.

These are duplicated here as constants (rather than fetched at runtime) so
parser code fails loudly (ImportError / NameError) on a typo instead of
silently writing a ``card`` value that doesn't match any real account. If a
name is ever renamed in Supabase, update it here to match.
"""

import re

PANAMA_DEBIT = 'Panama debit account (...0794)'
CITI_AADVANTAGE = 'Citi AAdvantage Platinum (...5130)'
CAPONE_QUICKSILVER = 'Capital One Quicksilver (...5636)'
CITI_COSTCO = 'Citi Costco Anywhere Visa (...1466)'
HUNTINGTON_CHECKING = 'Huntington Checking (...3491)'
AMEX_CONNECTMILES = 'AMEX ConnectMiles (...4473)'
BANCO_GENERAL_TRANSFERS = 'Banco General transfers (Panama)'
ROBINHOOD_SPENDING = 'Robinhood spending account'
CAPONE_360_SAVINGS = 'Capital One 360 Savings (...9130)'
PANAMA_MASTERCARD_2849 = 'Panama Mastercard (...2849)'
PANAMA_MASTERCARD_3029 = 'Panama Mastercard (...3029)'
CAPONE_360_CHECKING = 'Capital One 360 Checking (...9493)'
ROBINHOOD_VISA = 'Robinhood Visa (...9669)'

ALL = [
    PANAMA_DEBIT, CITI_AADVANTAGE, CAPONE_QUICKSILVER, CITI_COSTCO,
    HUNTINGTON_CHECKING, AMEX_CONNECTMILES, BANCO_GENERAL_TRANSFERS,
    ROBINHOOD_SPENDING, CAPONE_360_SAVINGS, PANAMA_MASTERCARD_2849,
    PANAMA_MASTERCARD_3029, CAPONE_360_CHECKING, ROBINHOOD_VISA,
]

# Panama Mastercard: last-4 on the statement -> account name.
PANAMA_MC_BY_SUBCARD = {
    '2849': PANAMA_MASTERCARD_2849,
    '3029': PANAMA_MASTERCARD_3029,
}

# Capital One 360: last-4 on the statement -> account name.
CAPONE_360_BY_SUBCARD = {
    '9493': CAPONE_360_CHECKING,
    '9130': CAPONE_360_SAVINGS,
}

# Last 4 digits as printed on a statement -> account name. Used by the AI
# extraction fallback (parsers/llm_extract.py), which only ever books rows
# onto an account it can resolve here. Most accounts' last 4 is in their
# name; the extras are account numbers printed on statements that differ
# from the card number in the name.
ACCOUNT_BY_LAST4 = {
    m.group(1): name for name in ALL
    for m in [re.search(r'\(\.\.\.(\d{4})\)', name)] if m
}
ACCOUNT_BY_LAST4.update({
    '4474': AMEX_CONNECTMILES,         # BAC account 3702-****-****-4474
    '4481': AMEX_CONNECTMILES,         # Trevor's supplementary AMEX card
    '0784': BANCO_GENERAL_TRANSFERS,   # Banco General savings 04-97-96-432078-4
})

# Credit cards: the printed balance is what's OWED, so it rises with
# charges. Everything else is a deposit account (balance falls with money
# out). The AI fallback reconciles the two kinds with opposite signs.
CARD_ACCOUNTS = {
    CITI_AADVANTAGE, CAPONE_QUICKSILVER, CITI_COSTCO, AMEX_CONNECTMILES,
    PANAMA_MASTERCARD_2849, PANAMA_MASTERCARD_3029, ROBINHOOD_VISA,
}
