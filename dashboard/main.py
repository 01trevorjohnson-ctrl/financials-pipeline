"""Household finance dashboard -- minimal FastAPI app, mobile-first plain
HTML/CSS, gated behind a single shared password (DASHBOARD_PASSWORD) via a
signed session cookie. Three pages: Summary (Flow + Category totals),
Needs Review (resolve queued transactions), and Assets & Liabilities
(read-only). See README.md for deployment + env vars.

This is a long-running web service (unlike the pipeline, which is a cron
job) -- Railway should run it as a normal Web Service, not a Cron Job.
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Form, Depends
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.staticfiles import StaticFiles

import db

APP_DIR = os.path.dirname(os.path.abspath(__file__))
templates = Jinja2Templates(directory=os.path.join(APP_DIR, 'templates'))

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
        return RedirectResponse('/summary', status_code=302)
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
        return RedirectResponse('/summary', status_code=302)
    return templates.TemplateResponse('login.html', {'request': request, 'error': 'Wrong password.'},
                                       status_code=401)


@app.get('/logout')
def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/login', status_code=302)


@app.get('/')
def root(request: Request):
    return RedirectResponse('/summary', status_code=302)


# ---------------------------------------------------------------------------
# Summary: Flow + Category totals
# ---------------------------------------------------------------------------
@app.get('/summary', response_class=HTMLResponse)
def summary(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    # Pull the columns we need for aggregation. Financial history here is a
    # couple thousand rows at most, so a client-side groupby is simpler and
    # plenty fast -- no need for a Postgres RPC/view.
    resp = client.table('transactions').select('amount, flow, category, date').execute()
    rows = resp.data or []

    flow_totals = {}
    category_totals = {}
    for r in rows:
        amt = r.get('amount') or 0
        flow = r.get('flow') or 'Unknown'
        cat = r.get('category') or 'Unknown'
        f = flow_totals.setdefault(flow, {'rows': 0, 'sum': 0.0})
        f['rows'] += 1
        f['sum'] += amt
        c = category_totals.setdefault(cat, {'rows': 0, 'sum': 0.0, 'flow': flow})
        c['rows'] += 1
        c['sum'] += amt

    flow_list = sorted(flow_totals.items(), key=lambda kv: -kv[1]['sum'])
    category_list = sorted(category_totals.items(), key=lambda kv: -abs(kv[1]['sum']))

    total_spend = flow_totals.get('Spend', {}).get('sum', 0.0)
    total_income = -flow_totals.get('Income', {}).get('sum', 0.0)

    return templates.TemplateResponse('summary.html', {
        'request': request, 'flow_list': flow_list, 'category_list': category_list,
        'total_spend': total_spend, 'total_income': total_income, 'row_count': len(rows),
    })


# ---------------------------------------------------------------------------
# Needs Review
# ---------------------------------------------------------------------------
@app.get('/needs-review', response_class=HTMLResponse)
def needs_review_list(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    resp = (client.table('needs_review').select('*')
            .eq('status', 'open').order('created_at', desc=True).execute())
    items = resp.data or []

    cats_resp = client.table('category_keys').select('category').eq('active', True).execute()
    categories = sorted({r['category'] for r in (cats_resp.data or [])})

    return templates.TemplateResponse('needs_review.html', {
        'request': request, 'items': items, 'categories': categories,
    })


@app.post('/needs-review/{review_id}/resolve')
def needs_review_resolve(request: Request, review_id: str, category: str = Form(...)):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    review_resp = client.table('needs_review').select('*').eq('id', review_id).limit(1).execute()
    review_rows = review_resp.data or []
    if not review_rows:
        return RedirectResponse('/needs-review', status_code=302)
    review = review_rows[0]

    # Look up the flow that goes with this category from category_keys, so
    # we don't leave a stale/mismatched flow on the transaction.
    flow = None
    ck_resp = (client.table('category_keys').select('flow').eq('category', category)
               .limit(1).execute())
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
        'resolved_at': datetime.now(timezone.utc).isoformat(),
    }).eq('id', review_id).execute()

    return RedirectResponse('/needs-review', status_code=302)


# ---------------------------------------------------------------------------
# Assets & Liabilities (read-only)
# ---------------------------------------------------------------------------
@app.get('/assets', response_class=HTMLResponse)
def assets(request: Request):
    if not require_auth(request):
        return RedirectResponse('/login', status_code=302)

    client = db.get_client()
    resp = client.table('assets_liabilities').select('*').order('as_of_date', desc=True).execute()
    rows = resp.data or []

    assets_rows = [r for r in rows if (r.get('item_type') or '').lower() != 'liability']
    liability_rows = [r for r in rows if (r.get('item_type') or '').lower() == 'liability']
    total_assets = sum(r.get('amount') or 0 for r in assets_rows)
    total_liabilities = sum(r.get('amount') or 0 for r in liability_rows)

    return templates.TemplateResponse('assets.html', {
        'request': request, 'assets_rows': assets_rows, 'liability_rows': liability_rows,
        'total_assets': total_assets, 'total_liabilities': total_liabilities,
        'net_worth': total_assets - total_liabilities,
    })


@app.get('/healthz')
def healthz():
    return {'ok': True}
