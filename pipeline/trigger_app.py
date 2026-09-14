"""Minimal HTTP trigger for on-demand pipeline runs.

Railway **Web Service** (always-on, unlike the pipeline's own Cron Job
service), started with:

    python -m uvicorn pipeline.trigger_app:app --host 0.0.0.0 --port $PORT

Exists so a caller with no Python runtime of its own -- the fam-fin Next.js
app's "Run Pipeline Now" button, specifically -- can trigger a pipeline run
over plain HTTP instead of importing ``pipeline.main.run_pipeline()``
in-process the way the (being-decommissioned) FastAPI dashboard does.

Auth is a single shared bearer token (``PIPELINE_TRIGGER_SECRET``), not a
per-user scheme: the caller here is a server (fam-fin's own backend, using
its own server-only env var), not a browser, so there's no session/cookie
to check and nothing per-user to distinguish. Same "keep this simple, not
enterprise-grade" posture as the dashboard's own single-password gate.

This module only adds an HTTP entrypoint. It does not change
``run_pipeline()`` or anything else about pipeline behavior.
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from .main import PipelineRunResult, run_pipeline

logger = logging.getLogger(__name__)

TRIGGER_SECRET = os.environ.get('PIPELINE_TRIGGER_SECRET')

app = FastAPI(title='Financials Pipeline Trigger')


def _check_auth(authorization: str | None) -> None:
    if not TRIGGER_SECRET:
        raise HTTPException(
            status_code=500,
            detail='PIPELINE_TRIGGER_SECRET is not configured on the server.',
        )
    if not authorization or not authorization.startswith('Bearer '):
        raise HTTPException(status_code=401, detail='Missing bearer token.')
    token = authorization[len('Bearer '):]
    # Constant-time comparison: this token is a long-lived shared secret, so
    # a timing side-channel is worth closing even in a two-user household app.
    if not hmac.compare_digest(token, TRIGGER_SECRET):
        raise HTTPException(status_code=401, detail='Invalid bearer token.')


def _result_to_dict(result: PipelineRunResult) -> dict:
    return {
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
        'summary_lines': result.summary_lines(),
    }


@app.get('/healthz')
def healthz():
    return {'ok': True}


@app.post('/run')
def run(authorization: str | None = Header(default=None)):
    _check_auth(authorization)

    try:
        result = run_pipeline()
    except Exception as e:
        logger.exception('Triggered pipeline run crashed unexpectedly')
        return JSONResponse(
            status_code=500,
            content={
                'ok': False,
                'crashed': True,
                'error': str(e),
                'started_at': None,
                'finished_at': None,
                'files_found': 0,
                'files_processed': 0,
                'files_skipped': 0,
                'transactions_inserted': 0,
                'needs_review_queued': 0,
                'unexpected_errors': 1,
                'failed': [],
                'summary_lines': [f'Pipeline crashed before completing: {e}'],
            },
        )

    return _result_to_dict(result)
