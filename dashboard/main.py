"""Household finance dashboard -- FastAPI + Jinja2, gated behind a single
shared password (DASHBOARD_PASSWORD) via a signed session cookie.

Four pages behind a bottom tab bar: Home (status + "Run Pipeline Now"),
Review (the needs_review flow), Trends (month-by-category spend chart), and
Ask (plain-English Q&A backed by the Claude API). See README.md for the
full page list, deployment, and env vars.

This is a long-running web service (unlike the pipeline, which is a cron
job) -- Railway should run it as a normal Web Service, not a Cron Job. As
of this version it ALSO imports and can invoke the pipeline package
directly (see /run-pipeline below), which is why GOOGLE_SERVICE_ACCOUNT_KEY
must now be set on this service too, not just the pipeline's.
"""
from __future__ import annotations

import hashlib
import os

import datetime

from fastapi import FastAPI, Request, Form
from fastapi.responses import RedirectResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

from . import ask as ask_backend
from . import colors
from . import coverage as coverage_backend
from . import db
from . import export as export_backend
from pipeline.main import run_pipeline

APP_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(APP_DIR, 'templates'))
templates.env.globals['category_color'] = colors.get_category_color
templates.env.globals['FINANCES_COLOR'] = colors.FINANCES_COLOR
templates.env.globals['SAVINGS_COLOR'] = colors.SAVINGS_COLOR

DASHBOARD_PASSWORD = os.environ.get('DASHBOARD_PASSWORD')
# A dedicated session secret is preferred (set SESSION_SECRET in Railway),
# but if it's not set we derive a stable one from the dashboard password so
# sessions still work without an extra required env var. Because it's
# derived from a value already treated as a secret, this is fine for a
# two-user household app; set SESSION_SECRET explicitly for a bit more
# separation between "the password" and "the cookie-signing key".
SESSION_SECRET = os.environ.get('SESSION_SECRET') or hashlib.sha256(
    (DASHBOARD_PASSWORD or 'insecure-dev-secret-change-me').encode()).hexdigest()

app = FastAPI(title='Household Finances')
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site='lax', max_age=60 * 60 * 24 * 30)
if os.path.isdir(os.path.join(APP_DIR, 'static')):
    app.mount('/static', StaticFiles(directory=os.path.join(APP_DIR, 'static')), name='static')


def require_auth(request: Request):
    if not request.session.get('authenticated'):
        return None
    return True


@app.get('/login', response_class=HTMLResponse)
def login_form(request: Request):
    if request.session.get('authenticated'):
        return RedirectResponse('/home', status_code=302)
    return templates.TemplateResponse('login.html', {'request': request, 'error': None})


@app.post('/login')
def login_submit(request: Request, password: str = Form(...)):
    if not DASHBOARD_PASSWORD:
        return templates.TemplateResponse('login.html', {
            'request': request,
            'error': 'DASHBOARD_PASSWORD is not configured on the server. Set it in Railway.'},
            status_code=500)
    if password == DASHBOARD_PASSWORD:
        request.session['authenticated'] = True
        return RedirectResponse('/home', status_code=302)
    return templates.TemplateResponse('login.html', {'request': request, 'error': 'Wrong password.'},
                                       status_code=401)


@app.get('/logout')
def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/login', status_code=302)


@app.get('/')
def root(request: Request):
    return RedirectResponse('/home', status_code=302)


# ---------------------------------------------------------------------------
# Home: status summary + "Run Pipeline Now"
# ---------------------------------------------------------------------------
@app.get('/home', response_class=HTMLResponse)
def home(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    last_activity = db.get_last_pipeline_activity(client)
    open_review_count = db.get_open_needs_review_count(client)

    account_coverage = coverage_backend.get_account_coverage(client)
    coverage_headline = coverage_backend.llm_headline(account_coverage)

    # A just-triggered run's result, stashed in the session by /run-pipeline
    # (redirect-with-flash-message pattern) -- shown once, then cleared.
    run_flash = request.session.pop('last_run_result', None)

    return templates.TemplateResponse('home.html', {
        'request': request, 'last_activity': last_activity,
        'open_review_count': open_review_count, 'run_flash': run_flash,
        'account_coverage': account_coverage, 'coverage_headline': coverage_headline,
        'active_tab': 'home',
    })


@app.post('/run-pipeline')
def run_pipeline_now(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    try:
        result = run_pipeline()
        flash = {
            'ok': result.ok,
            'started_at': result.started_at.isoformat(timespec='seconds'),
            'finished_at': result.finished_at.isoformat(timespec='seconds'),
            'files_found': result.files_found,
            'files_processed': result.files_processed,
            'files_skipped': result.files_skipped,
            'transactions_inserted': result.total_transactions_inserted,
            'needs_review_queued': result.total_needs_review_queued,
            'unexpected_errors': result.unexpected_errors,
            'failed': [{'filename': f.filename, 'detail': f.detail} for f in result.files_failed],
        }
    except Exception as e:
        flash = {
            'ok': False, 'crashed': True, 'error': str(e),
            'started_at': None, 'finished_at': None, 'files_found': 0, 'files_processed': 0,
            'files_skipped': 0, 'transactions_inserted': 0, 'needs_review_queued': 0,
            'unexpected_errors': 1, 'failed': [],
        }

    request.session['last_run_result'] = flash
    return RedirectResponse('/home', status_code=302)


# ---------------------------------------------------------------------------
# Needs Review: resolve flagged transactions (route path kept as
# /needs-review; only the nav label/styling changed to "Review")
# ---------------------------------------------------------------------------
@app.get('/needs-review', response_class=HTMLResponse)
def review_list(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    items = db.get_open_needs_review(client)
    categories = db.get_active_categories(client)

    return templates.TemplateResponse('needs_review.html', {
        'request': request, 'items': items, 'categories': categories, 'active_tab': 'review',
    })


@app.post('/needs-review/{review_id}/resolve')
def review_resolve(request: Request, review_id: str, category: str = Form(...)):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    db.resolve_needs_review(client, review_id, category)
    return RedirectResponse('/needs-review', status_code=302)


# ---------------------------------------------------------------------------
# Trends: month x category spend
# ---------------------------------------------------------------------------
@app.get('/trends', response_class=HTMLResponse)
def trends(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    monthly_by_category = db.get_monthly_spend_by_category(client)
    months = db.month_range(monthly_by_category)
    default_categories = db.top_categories_by_total_spend(monthly_by_category, n=6)
    all_categories = sorted(monthly_by_category.keys())

    series = []
    for cat in all_categories:
        months_dict = monthly_by_category[cat]
        series.append({
            'category': cat,
            'color': colors.get_category_color(cat),
            'total': round(sum(months_dict.values()), 2),
            'data': [round(months_dict.get(m, 0.0), 2) for m in months],
            'default_on': cat in default_categories,
        })
    # Largest categories first in the checkbox list.
    series.sort(key=lambda s: -s['total'])

    return templates.TemplateResponse('trends.html', {
        'request': request,
        'months': months, 'month_labels': [db.month_label(m) for m in months],
        'series': series, 'active_tab': 'trends',
    })


# ---------------------------------------------------------------------------
# Ask: plain-English Q&A backed by the Claude API
# ---------------------------------------------------------------------------
@app.get('/ask', response_class=HTMLResponse)
def ask_form(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    return templates.TemplateResponse('ask.html', {
        'request': request, 'configured': ask_backend.is_configured(),
        'question': None, 'result': None, 'error': None, 'active_tab': 'ask',
    })


@app.post('/ask', response_class=HTMLResponse)
def ask_submit(request: Request, question: str = Form(...)):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    if not ask_backend.is_configured():
        return templates.TemplateResponse('ask.html', {
            'request': request, 'configured': False,
            'question': question, 'result': None, 'error': None, 'active_tab': 'ask',
        })

    error = None
    result = None
    try:
        client = db.get_client()
        result = ask_backend.answer_question(client, question)
    except Exception as e:
        error = f'Could not get an answer: {e}'

    return templates.TemplateResponse('ask.html', {
        'request': request, 'configured': True,
        'question': question, 'result': result, 'error': error, 'active_tab': 'ask',
    })


@app.get('/export', response_class=HTMLResponse)
def export_page(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    return templates.TemplateResponse('export.html', {'request': request, 'active_tab': None})


@app.get('/export/transactions.csv')
def export_transactions_csv(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    rows = db.get_all_transactions_for_export(client)
    csv_text = export_backend.transactions_csv(rows)
    filename = f'transactions-{datetime.date.today().isoformat()}.csv'
    return Response(content=csv_text, media_type='text/csv',
                     headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/export/full.json')
def export_full_json(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    bundle = db.get_full_export_bundle(client)
    json_text = export_backend.full_bundle_json(bundle)
    filename = f'financials-export-{datetime.date.today().isoformat()}.json'
    return Response(content=json_text, media_type='application/json',
                     headers={'Content-Disposition': f'attachment; filename="{filename}"'})


@app.get('/healthz')
def healthz():
    return {'ok': True}
