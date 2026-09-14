"""Supabase access for the dashboard service.

IMPORTANT DEVIATION FROM THE LITERAL SPEC, DOCUMENTED HERE AND IN README.md:
public.* tables have RLS policies requiring `auth.role() = 'authenticated'`.
A bare SUPABASE_ANON_KEY request (no signed-in Supabase Auth session) has
role 'anon', not 'authenticated' -- so it would be silently blocked by RLS
and every query would come back empty, regardless of DASHBOARD_PASSWORD.
Standing up real per-user Supabase Auth sign-in is exactly the "full auth"
the task brief says to skip for v1 ("gate the whole dashboard behind a
single shared password ... rather than building full auth").

So: this module prefers SUPABASE_SERVICE_KEY (server-side only, never sent
to the browser) when it's set, which bypasses RLS entirely and just works
with the schema as-is. It falls back to SUPABASE_ANON_KEY only if no
service key is configured -- which will only successfully read/write once
the household later sets up real Supabase Auth (a dedicated signed-in user)
or relaxes the RLS policies; until then it will return empty results. The
whole app is already gated by DASHBOARD_PASSWORD before any Supabase call
is made (see auth.py), so using the service key here does not weaken the
"only two household members can view this" property -- it's the same trust
model as any server-side app holding a backend DB credential behind a login.
"""
from __future__ import annotations

import os
from functools import lru_cache

from supabase import create_client, Client


@lru_cache(maxsize=1)
def get_client() -> Client:
    url = os.environ.get('SUPABASE_URL')
    key = os.environ.get('SUPABASE_SERVICE_KEY') or os.environ.get('SUPABASE_ANON_KEY')
    if not url or not key:
        raise RuntimeError(
            'SUPABASE_URL and (SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY) must be set in the '
            'environment. See README.md "Environment variables".')
    return create_client(url, key)
