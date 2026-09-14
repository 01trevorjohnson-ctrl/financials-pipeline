"""Household finance pipeline package.

Both Railway services (pipeline cron job + dashboard web service) now build
from the repo root, so this directory is a proper importable package
(``pipeline``) rather than a script directory that only worked when the
process's cwd was ``pipeline/``. See README.md section 3 for the exact
Start Commands.
"""
