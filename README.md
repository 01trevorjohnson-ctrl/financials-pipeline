# Financials Pipeline

Automates the Johnson/Suarez household's monthly finance ledger. Two
independent Railway *services* sharing one repo AND, as of this version,
one Python package tree at the repo root (see "Architecture" below):

- **`pipeline/`** -- importable package whose `run_pipeline()` (in
  `pipeline/main.py`) is the pipeline's entire logic, run either on a
  schedule (Railway **Cron Job**) or on demand from the dashboard's "Run
  Pipeline Now" button. Watches a shared Google Drive folder for new
  bank/card statements, parses them, reconciles the parsed transactions
  against each statement's own printed totals, categorizes every row, and
  writes everything to Supabase. Never guesses: anything it can't
  confidently parse, reconcile, or categorize is left for a human (file
  stays in Drive; a `needs_review` row is queued).
- **`dashboard/`** -- a FastAPI + Jinja2 web app (Railway **Web Service**,
  always-on), password-gated, styled to match the FAMILY app's "budget"
  visual language (see "UI" below). Four mobile-first pages behind a
  bottom tab bar: **Home** (pipeline status + "Run Pipeline Now"),
  **Review** (resolve flagged transactions, at `/needs-review`), **Trends**
  (month-by-category spend chart), and **Ask** (plain-English Q&A backed
  by the Claude API). The old Summary and Assets & Liabilities pages were
  removed per the household's request; their underlying `dashboard/db.py`
  query patterns and the Supabase tables themselves are untouched.

Data lives in Supabase Postgres, project `family-finance`
(`wfpaakmjveuhugskqmup`). This repo does not create or alter that schema.

---

## 0. Architecture: both services now build from the repo root

Both Railway services used to have their Root Directory set to their own
subfolder (`pipeline` / `dashboard`), each with its own
`requirements.txt`/`.python-version`, and each subfolder's Python files
used plain top-level imports (`import db`, `import accounts`, ...) that
only worked because the process's cwd was that subfolder.

That changed to let the dashboard's "Run Pipeline Now" button (see section
2) import and call the pipeline's own `run_pipeline()` function directly,
in-process -- which requires both `pipeline/` and `dashboard/` to be
regular importable Python packages (each now has an `__init__.py`) living
under one shared import root. Consequences, all already done in this repo:

- **One `requirements.txt` and one `.python-version` at the repo root**
  (the union of the old `pipeline/requirements.txt` and
  `dashboard/requirements.txt`, plus `anthropic` for the Ask page) --
  the two subfolder copies of each file are gone.
- **Both services' Root Directory must be changed to the repo root** (`.`
  or blank), not `pipeline` / `dashboard`, so Nixpacks sees the root
  `requirements.txt` and both packages.
- **Both services' Start Command must change** to run the app as a module
  from the repo root, so Python's package/import machinery resolves
  `pipeline.*` / `dashboard.*` correctly:
  - pipeline (Cron Job): `python -m pipeline.main`
  - dashboard (Web Service): `python -m uvicorn dashboard.main:app --host 0.0.0.0 --port $PORT`
    (the `python -m` form, not a bare `uvicorn ...`, guarantees the repo
    root is on `sys.path` regardless of how uvicorn's own CLI resolves
    `--app-dir`)
- **`GOOGLE_SERVICE_ACCOUNT_KEY` must now also be set on the dashboard
  service**, not just the pipeline's -- see the env var table below.

Internally, every former top-level import inside `pipeline/` (`import db`,
`import accounts`, `from categorize import ...`, `from parsers import
...`) is now a package-relative import (`from . import db`, `from .. import
accounts`, `from .categorize import ...`, `from .parsers import ...`), and
`dashboard/main.py`'s `import db` is now `from . import db`. This was
necessary, not just tidiness: once the dashboard process imports the
`pipeline` package into the same Python process (to call `run_pipeline()`),
a plain top-level `import db` from *either* package would collide in
`sys.modules` -- Python caches modules by name, so `pipeline/main.py`'s
`import db` and `dashboard/main.py`'s `import db` would silently resolve
to whichever one loaded first, handing the pipeline the dashboard's `db.py`
(or vice versa) with no error, just wrong behavior. Explicit relative
imports give each package its own `pipeline.db` / `dashboard.db` names, so
there's no collision.

**None of this changes pipeline *behavior*** -- `pipeline/main.py`'s
`run_pipeline()` contains the exact same logic as the old `main()`, still
writes the same rows, still preserves the exact original cron exit-code
contract (see section 1).

---

## 1. How the pipeline works

1. Lists files sitting directly in the Drive root folder **"Johnson Suarez
   Financials"**. New statements get dropped here (e.g. from a phone).
2. For each file, checks `processed_statements.drive_file_id`. Already
   `status='processed'`? Skip. Otherwise it's new (or a previous attempt
   errored) -- process it.
3. **Detects the format** by filename pattern first (mirroring the "Source
   Documents (Standardized Names)" naming convention -- "AMEX",
   "AAdvantage", "Costco", "Quicksilver", "Huntington", "360 Checking/
   Savings", "Panama Mastercard" / `2849`/`3029`, "BAC"/"debit"/`0794`,
   "Banco General"/"transfers", "Robinhood Visa"/`9669`, "Robinhood
   spending"), then falls back to sniffing file content (header rows, PDF
   text markers) if the filename doesn't clearly match. See
   `pipeline/parsers/__init__.py` (`REGISTRY`, `detect_parser`).
4. **Parses** it with the matched module in `pipeline/parsers/`.
5. **Reconciles**: sum of parsed transaction amounts (+ previous balance,
   where the statement has one) must match the statement's own printed
   ending balance/cutoff within **$0.02**. If it doesn't, nothing is
   inserted -- `processed_statements` gets `status='error'`,
   `reconciliation_ok=false`, and the diff in `reconciliation_detail`; the
   file is left in the Drive root untouched so a human notices.
   *(Two source formats -- the Robinhood spending CSV export and the Banco
   General transfers PDF -- have no independently-printed total or balance
   at all, by the household's own notes. Those two do a structural sanity
   check instead and are documented as an explicit exception; see the
   "Assumptions" section below.)*
6. **Categorizes** every row against `category_keys` (ascending
   `priority`, first keyword match wins), plus the special cases from
   `category_keys.notes` that aren't plain keyword matching -- implemented
   in `pipeline/categorize.py`:
   - Self-transfer vs. gift: a Zelle/ACH transfer to **Trevor Johnson**
     himself is `Account transfer`/`Transfer`, never a gift, checked
     *before* the generic keyword loop.
   - Wise transfers to Colombia: not split -- each whole row alternates
     `Charitable giving` / `Investment`, ordered by date, seeded from what's
     already in the database so the alternation stays balanced across
     separate runs (not just within one statement).
   - `TOTAL ITBMS` (Panama VAT): produced by the CSV parsers as a single
     statement-level row, dated to the statement cutoff.
   - PedidosYa's two rows per order (order + tip): no special code needed,
     both match the `Delivery` category keyword directly.
   - Income vs. Refund precedence for "ACH CRE ..." lines: handled
     automatically by `category_keys.priority` ordering.
7. Anything matching **no** keyword goes through one more step before
   giving up: a single batched Claude API call per statement (see "LLM
   categorization fallback" below), classifying every keyword-unmatched row
   in that statement at once, constrained to the household's real category
   list. Whatever's still unclassified after that -- because the model
   itself said "Uncategorized", because `ANTHROPIC_API_KEY` isn't set on
   the pipeline service, or because the call failed for any reason -- is
   `category='Uncategorized'` with a best-effort `flow` guess from the
   transaction's type. Per statement: if the *sum* of that statement's
   remaining Uncategorized amounts is **>= $50**, every Uncategorized row
   from that statement is queued into `needs_review` (this single check
   subsumes "any single row >= $50", since the sum is always >= any
   individual row -- see `pipeline/categorize.py` docstring). **This $50
   threshold is adjustable** -- it's `NEEDS_REVIEW_THRESHOLD` in
   `pipeline/categorize.py`.

### LLM categorization fallback

`category_keys` keyword matching is fast and free but only ever catches
*exact* substrings -- a real statement has one-off merchant strings
(specific restaurant names, format variants like `"UBER *TRIP"` vs.
`"UBER TRIP"`) that no reasonably-sized keyword list will ever fully
enumerate. Rather than dumping all of those into manual review every time,
`pipeline/categorize.py: llm_categorize_uncategorized()` makes **one
batched Claude call per statement** covering every row the keyword loop
left as Uncategorized, asking the model to pick from the household's own
real category list (built from `category_keys`, not hardcoded -- the model
is constrained via JSON-schema `enum` and can never invent a category).
Rows the model can't confidently place stay "Uncategorized" and flow into
the existing `needs_review` threshold above, same as before this existed.

This degrades gracefully and never blocks a pipeline run: if
`ANTHROPIC_API_KEY` isn't set on the pipeline service, if the API call
fails, or if the response doesn't parse, the function catches it, logs a
warning, and returns nothing to override -- every affected row simply
falls through to the pre-existing Uncategorized + needs_review behavior.
Model: `claude-haiku-4-5`, same choice as the dashboard's Ask page (see
below) -- a small/fast model is the right fit for a constrained
classification call, not open-ended reasoning. **`ANTHROPIC_API_KEY` must
be set on the pipeline service** (not just the dashboard's) for this
fallback to actually run in production -- see the env var table below.
8. On success: inserts `processed_statements` (`status='processed'`), all
   `transactions` rows (with `statement_id` set), any `needs_review` rows;
   then in Drive, moves+renames the original (now in root) into
   **"Source Documents (Standardized Names)"**, e.g.
   `"2026-05 - AMEX ConnectMiles (...4473) statement.csv"`. There is no
   automated copy into "Raw Originals (Archived)" -- see the platform
   limitation below.

### Platform limitation: no automated "Raw Originals (Archived)" copy

Google Drive service accounts have **zero storage quota** on a personal
(non-Workspace) Drive. Creating any new file content -- `files().copy()`,
any upload -- fails with a 403 `storageQuotaExceeded` ("Service Accounts do
not have storage quota"), no matter which folder it targets. This isn't
something a request parameter can work around; Google's own suggested fixes
(Shared Drives, domain-wide delegation) both require Google Workspace, which
this account doesn't have. The only Drive writes a service account *can* do
here are metadata-only (move/rename), which is why the pipeline does a
single move+rename straight into "Source Documents (Standardized Names)" and
does not also duplicate the file into "Raw Originals (Archived)" -- that
folder only holds what was filed there by hand before this pipeline existed.
If a genuine byte-identical archived copy ever matters, the options are:
upgrade to Google Workspace (Shared Drives pool storage instead of relying
on any one account's quota), or copy files by hand occasionally.

`run_pipeline()` (in `pipeline/main.py`) contains all of the above and
returns a `PipelineRunResult` (files found/processed/skipped/failed,
transactions inserted, needs_review rows queued) -- it never calls
`print()`/`sys.exit()`, only structured `logging`, so it's safe to call
from a long-running process. This is what lets the dashboard's "Run
Pipeline Now" button (see section 2) trigger an on-demand run of the exact
same logic as the cron job, in-process. The `if __name__ == '__main__':`
block at the bottom of `pipeline/main.py` is a thin wrapper: call
`run_pipeline()`, print a human-readable summary in the same style as the
original script's log output (since Railway's cron logs are how the
household verifies runs), exit 0/1. **The exit-code contract is
unchanged from the original script**: only a truly unexpected exception
(not a parse/reconciliation failure, which is expected/handled behavior)
makes a run exit non-zero.

Run it locally (with a populated `.env`, see below), from the repo root:

```bash
pip install -r requirements.txt
python -m pipeline.main
```

## 2. How the dashboard works

Four mobile-first pages behind a password form (`DASHBOARD_PASSWORD`,
checked against a signed session cookie -- no per-user accounts, per the
brief: "keep this simple, not enterprise-grade") and a glassy bottom tab
bar, styled to match the FAMILY app's own "budget" visual language exactly
(same `--bud-*` CSS custom property names, same Outfit/Manrope fonts, same
glass-panel/blob/starfield treatment -- see `dashboard/static/style.css`
and `dashboard/colors.py`, built by reading
`family-finance/src/app/globals.css`, `_shared/colors.ts`, and
`budget/fonts.ts` directly):

- **Home** (`/home`) -- a compact status summary (open `needs_review`
  count, last pipeline activity) plus the **Run Pipeline Now** button
  (`POST /run-pipeline`, redirect-with-flash-message pattern: the result
  is stashed in the signed session cookie and shown once on the next
  `/home` load). Calls `pipeline.main.run_pipeline()` synchronously --
  production log history shows a run takes seconds, not minutes, so no
  background-job infrastructure was added for this.
- **Review** (`/needs-review` -- route path kept from the original spec;
  only the nav label changed) -- open `needs_review` rows, each with a
  dropdown to assign a category (looked up against `category_keys` for
  the matching flow) and mark resolved; writes back to
  `transactions.category`/`.flow` and `needs_review.status='resolved'`.
  Unchanged functionality from before, restyled only.
- **Trends** (`/trends`) -- a Chart.js line chart of monthly Spend by
  category (`transactions` rows pulled via the same supabase-py table
  builder used everywhere else in this app, then bucketed by month in
  Python -- `date_trunc` grouping isn't expressible through the PostgREST
  builder, so this is the one Python-side aggregation, not a second raw-SQL
  path) with a checkbox list of every category actually present in the
  data, defaulting to the top 6 by total spend. Category colors come from
  `dashboard/colors.py` (see "Category colors" below).
- **Ask** (`/ask`) -- plain-English Q&A backed by the Claude API (see
  "The Ask page" below). Shows a "not configured yet" message instead of
  crashing if `ANTHROPIC_API_KEY` isn't set.

The old **Summary** and **Assets & Liabilities** pages were removed per
the household's request (item 2 of the six things they asked for). Their
routes/templates are gone; the `assets_liabilities` table and
`dashboard/db.py`'s general query patterns are untouched in case a future
page needs similar data again.

Run it locally, from the repo root:

```bash
pip install -r requirements.txt
python -m uvicorn dashboard.main:app --reload --port 8000
```

### Category colors

`dashboard/colors.py` reuses the FAMILY app's exact category hex values
(from `family-finance/src/app/budget/category-config.tsx`) for every
category name that means the same thing in both apps (e.g. "Groceries" is
`#3FA672` in both). For categories that exist only in this app's data
(`Rent`, `Travel`, `Student loans`, `Childcare & education`, `Gifts &
support to individuals`, `Utilities & internet`, `Taxes & professional`,
`Cash`, `Subscriptions & software`, `Insurance & fees`, `Charitable
giving`, `Uncategorized`), new colors were hand-picked to stay visually
distinct from the reused ones and from `FINANCES_COLOR`
(`#5568D8`, this app's own accent, reused verbatim from
`family-finance/src/app/_shared/colors.ts`) and `SAVINGS_COLOR`
(`#D4A72C`). Any category not in that table (e.g. a brand-new
`category_keys` row added later) gets a deterministic color instead of a
crash, generated from a hash of its name.

### The Ask page

Two Claude API calls, never raw SQL against the service-role-authenticated
Supabase connection (`SUPABASE_SERVICE_KEY` bypasses RLS entirely, so
LLM-generated SQL would be a real injection/safety risk):

1. `dashboard/ask.py: build_filter_spec()` -- gives Claude the question,
   the available fields, and the REAL distinct `category`/`flow` values
   (queried from Supabase, not hardcoded), and asks for a small structured
   JSON filter spec (`metric`, `date_from`, `date_to`, `categories`,
   `flow`, `group_by`), guaranteed-valid JSON via `output_config.format`
   (a JSON Schema). `validate_spec()` then whitelists every field
   server-side (categories/flow against the real queried values, dates
   parsed and range-checked) before it's used for anything.
2. The validated spec runs through `dashboard/db.py: run_ask_query()` --
   the same supabase-py `.table(...).select(...)` builder pattern as every
   other query in this app -- then `compute_metric()` sums/counts/averages
   the result in Python, optionally grouped by category or month.
3. `dashboard/ask.py: phrase_answer()` -- a second Claude call, given the
   original question plus the actual computed numbers, phrases a short
   natural-language answer. The underlying numbers are always shown
   alongside it on the page, not just the LLM's sentence.

Model: `claude-haiku-4-5` for both calls -- a small/fast model is the
right cost/latency tradeoff here since both calls are simple (a short JSON
extraction, then a one-paragraph phrasing), not open-ended reasoning.

### Important: why the dashboard prefers `SUPABASE_SERVICE_KEY` over the anon key

Every table has RLS requiring `auth.role() = 'authenticated'`. A bare
`SUPABASE_ANON_KEY` request with no signed-in Supabase Auth session has
role `anon`, not `authenticated` -- so RLS would silently block every
query, regardless of `DASHBOARD_PASSWORD`. Standing up real per-user
Supabase Auth sign-in is exactly the "full auth" the brief says to skip for
v1. So `dashboard/db.py` prefers `SUPABASE_SERVICE_KEY` (server-side only,
never sent to the browser) when set -- it bypasses RLS and just works with
the schema as-is, behind the same `DASHBOARD_PASSWORD` gate. It falls back
to `SUPABASE_ANON_KEY` only if no service key is configured, which will
only actually read/write once real Supabase Auth is added later. **Set
`SUPABASE_SERVICE_KEY` on the dashboard service too** (see env var table
below) -- this is a deliberate, documented deviation from a literal
anon-key-only reading of the brief, made necessary by the existing RLS
policies, which this repo does not alter.

---

## 3. Deploying to Railway (both services, one repo)

You'll do this manually in Railway's dashboard -- no `railway` CLI is used
or required anywhere in this repo.

### 3.0 Push this repo to GitHub first

This repo has already been `git init`'d with an initial commit (see bottom
of this README). You still need to:

```bash
# create a new repo on github.com (e.g. TrevorSJohnson/financials-pipeline), then:
cd financials-pipeline
git remote add origin https://github.com/<you>/financials-pipeline.git
git branch -M main
git push -u origin main
```

### 3.1 Create the pipeline service (Cron Job)

1. In Railway, open your project (or create a new one) -> **New -> GitHub
   Repo** -> pick this repo.
2. Railway will create one service from the repo root. Open its
   **Settings**:
   - **Service Name**: `financials-pipeline` (or similar).
   - **Root Directory**: the repo root (`.` / leave blank) -- **not**
     `pipeline`. See section 0: both services now build from the repo
     root so the dashboard can import the pipeline package in-process.
   - **Build**: leave on Nixpacks (auto-detected from the root
     `requirements.txt` and `.python-version`); no custom build command
     needed.
   - **Deploy -> Custom Start Command**: `python -m pipeline.main` --
     **not** `python main.py` or `python pipeline/main.py`. The `-m` form
     is required: it's what makes `pipeline/main.py`'s package-relative
     imports (`from . import db`, etc.) resolve correctly.
   - **Deploy -> Service Type**: change this service to **Cron Job** (not
     the default always-on Web Service) -- Railway's Cron Job type runs
     the start command on a schedule and exits, rather than expecting a
     listening process. In the Settings panel this is under
     **Cron Schedule**; Railway will prompt you to set one once the
     service type allows it.
   - **Cron Schedule**: enter a standard cron expression, e.g. daily at
     7am US-Central / noon UTC: `0 12 * * *`. Adjust to taste -- there's
     no harm running it more often (it's idempotent: already-processed
     files are skipped).
3. Set environment variables (Settings -> Variables) -- see the table
   below.
4. Deploy. Check **Deployments -> View Logs** after the first scheduled (or
   manually triggered) run -- the script logs every file it looks at,
   whether it reconciled, and what it wrote.

### 3.2 Create the dashboard service (Web Service)

1. In the same Railway project -> **New -> GitHub Repo** -> the same repo
   again (Railway supports multiple services from one repo, each with its
   own root directory).
2. Settings:
   - **Service Name**: `financials-dashboard` (or similar).
   - **Root Directory**: the repo root (`.` / leave blank) -- **not**
     `dashboard`. Same reason as above: this service now imports the
     `pipeline` package directly for the "Run Pipeline Now" button, so it
     needs both packages on its import path.
   - **Build**: Nixpacks auto-detected (root `requirements.txt`), no
     custom build command.
   - **Deploy -> Custom Start Command**:
     `python -m uvicorn dashboard.main:app --host 0.0.0.0 --port $PORT`
     -- the `python -m uvicorn` form (not a bare `uvicorn ...`) guarantees
     the repo root is on `sys.path` so `dashboard.main`'s package-relative
     imports and its `from pipeline.main import run_pipeline` both
     resolve.
   - **Service Type**: leave as the default **Web Service** (always-on;
     this is not a cron job).
3. Set environment variables -- see the table below. **Note the new
   `GOOGLE_SERVICE_ACCOUNT_KEY` and `ANTHROPIC_API_KEY` rows** -- both are
   new requirements on this service specifically (see section 0 and "The
   Ask page" above).
4. Deploy. Railway gives it a generated domain under **Settings ->
   Networking -> Public Networking** (looks like
   `financials-dashboard-production.up.railway.app`) -- click **Generate
   Domain** if one isn't there yet. Confirm `https://<that domain>/login`
   loads before moving on to the custom domain step.

### 3.3 Environment variables

| Service | Variable | Where to get it |
|---|---|---|
| pipeline | `SUPABASE_URL` | Supabase dashboard -> your project (`family-finance`) -> **Project Settings -> API** -> "Project URL". For this project it's `https://wfpaakmjveuhugskqmup.supabase.co`. |
| pipeline | `SUPABASE_SERVICE_KEY` | Same API settings page -> **Project API keys -> `service_role`** (labeled "secret"). Never expose this to a browser. |
| pipeline | `GOOGLE_SERVICE_ACCOUNT_KEY` | The full contents of the service-account JSON key file you already downloaded from Google Cloud Console (IAM & Admin -> Service Accounts -> your account -> Keys), pasted as a single-line string value. |
| pipeline | `GOOGLE_DRIVE_ROOT_FOLDER_ID` *(optional)* | Defaults to `1Vdnu5u9doehcyNdNZJLxvD7OTaYSPx8a` (the "Johnson Suarez Financials" folder). Only set this if the folder ever changes. |
| pipeline | `GOOGLE_DRIVE_STANDARDIZED_FOLDER_ID` *(optional)* | Defaults to `15bTjig6O9mUQ8TZ5jV1avfHSDA2LJl5r` ("Source Documents (Standardized Names)"). |
| pipeline | `ANTHROPIC_API_KEY` **(new)** | Same key as the dashboard's own (see below), from [console.anthropic.com](https://console.anthropic.com). Powers the LLM categorization fallback (see "LLM categorization fallback" above). If unset, the pipeline just skips that step -- keyword-unmatched rows go straight to Uncategorized/needs_review as before, no error. |
| dashboard | `SUPABASE_URL` | Same as above. |
| dashboard | `SUPABASE_SERVICE_KEY` | Same `service_role` key as the pipeline (see "why the dashboard prefers the service key" above). Server-side only. |
| dashboard | `SUPABASE_ANON_KEY` *(optional, forward-looking)* | Same API settings page -> **Project API keys -> `anon` `public`**. Currently unused unless `SUPABASE_SERVICE_KEY` is absent (see above); set it anyway so it's ready once real Supabase Auth is added. |
| dashboard | `DASHBOARD_PASSWORD` | Pick your own password for the household. Anyone with it can view and resolve review items. |
| dashboard | `SESSION_SECRET` *(optional)* | Any random string. If omitted, one is derived from `DASHBOARD_PASSWORD`; set this to decouple the cookie-signing key from the login password. |
| dashboard | `GOOGLE_SERVICE_ACCOUNT_KEY` **(new)** | Same value as the pipeline's own key above. Required now because the dashboard's "Run Pipeline Now" button imports and calls the pipeline package in-process, and the pipeline needs Drive access to do anything. |
| dashboard | `ANTHROPIC_API_KEY` **(new)** | From [console.anthropic.com](https://console.anthropic.com). Powers the Ask page (see "The Ask page" above). If unset, the Ask page shows a "not configured yet" message rather than crashing -- deploy without it first and add it whenever the household has a key. |

### 3.4 Custom domain: `finances.holamajordomo.com` -> the dashboard

DNS is at Namecheap; the app is on Railway. You'll add a CNAME record
pointing the subdomain at Railway's generated domain for the dashboard
service.

**In Railway:**
1. Open the **dashboard** service -> **Settings -> Networking -> Public
   Networking**.
2. Click **Custom Domain**, enter `finances.holamajordomo.com`, and submit.
3. Railway shows a CNAME target, something like
   `<random-id>.up.railway.app` (or it may say to point at the same
   generated domain from step 3.2) -- copy that exact value.

**In Namecheap:**
1. Log in -> **Domain List** -> next to `holamajordomo.com` click
   **Manage**.
2. Go to the **Advanced DNS** tab.
3. Click **Add New Record**:
   - **Type**: `CNAME Record`
   - **Host**: `finances`
   - **Value**: the target Railway showed you (include the trailing dot if
     Namecheap's UI requires it; usually not needed)
   - **TTL**: Automatic (or 5 min while testing)
4. Save. Do **not** also create an A record for the same host -- one
   record per host/subdomain.

**Back in Railway:** the custom domain will show "Verifying..." then
"Active" once DNS propagates (usually minutes, occasionally up to a few
hours). Railway auto-provisions the TLS certificate once it verifies.

Visit `https://finances.holamajordomo.com/login` once it's active to
confirm.

---

## 4. Data model quick reference (already in Supabase, not altered here)

- `accounts` -- 13 seeded rows, one per account. `pipeline/accounts.py`
  hardcodes the exact `name` strings as constants so a typo fails loudly
  instead of silently writing a `transactions.card` value matching no real
  account.
- `category_keys` -- 24 seeded rows (`category`, `flow`, `keywords[]`,
  `priority`, `notes`). Read fully by `pipeline/db.py` /
  `pipeline/categorize.py` on every pipeline run (not cached across runs,
  so editing rules in Supabase takes effect on the next run with no
  redeploy needed).
- `processed_statements` -- one row per Drive file, keyed by
  `drive_file_id` (unique). The idempotency manifest.
- `transactions` -- `amount`: **positive = money out (spend)**, **negative
  = money in (income/refund)**, matching the household's existing
  workbook convention.
- `needs_review` -- queue for anything the pipeline couldn't confidently
  categorize (see threshold above) or couldn't parse/reconcile at all.
- `assets_liabilities`, `income_paychecks` -- untouched by this pipeline;
  manual/occasional updates only. No longer surfaced in the dashboard UI
  (the `/assets` page was removed per the household's request), but the
  table itself is untouched in case a future page needs it again.

---

## 5. Assumptions made while building this (things the source material left ambiguous)

- **RLS + anon key**: see "why the dashboard prefers the service key"
  above -- a real, load-bearing deviation, not cosmetic.
- **"Raw Originals (Archived)" is no longer written to automatically**: the
  initial version of this pipeline tried to copy each original into that
  folder before moving/renaming it. In production this failed every time
  with a 403 `storageQuotaExceeded` -- Drive service accounts have no
  storage quota on a personal (non-Workspace) Drive, so they cannot create
  *any* new file content, and the copy's failure was also silently blocking
  the (unrelated, quota-free) move+rename from ever running, since both were
  wrapped in one try/except. Fixed by dropping the copy entirely and doing
  only the move+rename -- see the "Platform limitation" section above.
- **Standardized filename for a statement covering more than one account**:
  Capital One 360 (Checking+Savings in one PDF) and Panama Mastercard
  (primary ...2849 + supplementary ...3029 in one CSV) each span two
  `accounts` rows from a single Drive file. There was no existing example
  of this specific case in the "Source Documents (Standardized Names)"
  folder to match against, so `pipeline/naming.py` joins both account names
  with `" & "`, e.g.
  `"2026-03 - Panama Mastercard (...2849) & Panama Mastercard (...3029) statement.csv"`.
  Adjust `naming.build_standardized_filename` if the household prefers a
  different convention (or two separately-renamed copies) once they see a
  real example.
- **No independent statement total** (Robinhood spending CSV export, Banco
  General transfers PDF): both formats genuinely carry no printed
  ending-balance/total line per the household's own notes on the original
  source data. These two parsers do a structural sanity check (every row
  parses to a valid date + amount) instead of a true reconciliation, and
  say so explicitly in `reconciliation_detail`. Every other format (the 9
  remaining account types) reconciles against a real printed figure or an
  internally-consistent running balance column.
- **Needs-review threshold semantics**: "sum >= $50 OR any single row >=
  $50" is logically just "sum >= $50" (sum of absolute values is always >=
  any individual absolute value), and when it fires, *all* of that
  statement's Uncategorized rows get queued (not just the large one) --
  see `pipeline/categorize.py`. `$50` lives at
  `categorize.NEEDS_REVIEW_THRESHOLD`.
- **Wise alternation persistence**: seeded from existing DB rows
  (`db.get_wise_category_counts`) so a fresh pipeline run continues the
  household's existing ~50/50 split instead of restarting the alternation
  from scratch every time.
- **Session cookie secret**: derived from `DASHBOARD_PASSWORD` if
  `SESSION_SECRET` isn't set, purely to avoid one more required env var for
  a two-person household app. Set `SESSION_SECRET` explicitly if you'd
  rather they be unrelated.
- **"Last pipeline activity" on Home**: there's no dedicated pipeline-run
  log table (this repo still does not create/alter the Supabase schema),
  so `dashboard/db.py: get_last_pipeline_activity()` uses the most recent
  `processed_statements.processed_at` as a proxy. Caveat, worth knowing:
  `processed_at` is set once at insert and is not bumped by
  `update_processed_statement()` on a retry, and a run that finds zero new
  Drive files doesn't touch this table at all -- so it reflects "the last
  time a *file* was processed," not strictly "the last cron invocation."
  Good enough for a glance on Home; a real run-log table would be the
  cleaner fix if the household wants exact cron-run history later.
- **`/needs-review` route path kept as-is**: the brief said "keep
  `/needs-review`" while also asking for a bottom-tab-bar restructure with
  a "Review" tab -- read literally as keeping the URL (for any existing
  bookmarks/muscle memory) while only the nav label/styling changes to
  "Review". `/summary` and `/assets` were removed entirely (routes,
  templates, nav links) per the brief.
- **Chart.js version pin**: `dashboard/templates/trends.html` loads
  `https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.1/chart.umd.min.js`
  -- verified against cdnjs's own library API while building this (the
  cdnjs slug is capitalized `Chart.js`, and the file is `chart.umd.min.js`,
  not `chart.min.js`, for the UMD global the inline `<script>` expects).
- **Ask page model choice**: `claude-haiku-4-5` for both Claude calls, per
  this task's own brief ("a small/fast model is fine for both calls given
  the queries are simple") -- see `dashboard/ask.py`.
- **Category colors for FP-only categories**: hand-picked to stay visually
  distinct from the 8 reused FAMILY colors and from `FINANCES_COLOR`/
  `SAVINGS_COLOR`; see `dashboard/colors.py` and "Category colors" above.
  A category not in that table (future `category_keys` addition) gets a
  deterministic hash-based color instead of breaking.
- **`category_keys` keyword expansion, data-driven**: after the household
  reported real miscategorized rows (Uber variants landing Uncategorized,
  a Costco membership fee not matching Subscriptions), ran a gap-detection
  query against `transactions` (grouped by category/flow/merchant) joined
  against `category_keys.keywords` to find every merchant whose real,
  already-correct category would NOT be reproduced by the existing keyword
  list on a reprocess. Added ~50 new keywords across 10 categories from
  that result -- generalizable brand/merchant strings only (e.g. `'UBER
  *TRIP'`, `'COSTCO'`), deliberately skipping one-off (`n=1`) restaurant/
  merchant strings that wouldn't generalize to future statements, matching
  the household's own existing keyword style. Re-running the same
  gap-detection query afterward confirmed both the Uber and Costco cases
  are fully resolved; ~230 largely one-off merchants remain as an
  irreducible tail -- that tail is exactly what the LLM fallback above
  exists to catch instead of a keyword list chasing every one-off forever.

## 6. What I could NOT do from here (needs you)

- **Live-test against a real Drive service account key** -- I don't have
  the key (correctly -- it should never be pasted into a chat). Test with a
  real statement drop after deploying, and watch the pipeline service's
  logs on its first scheduled/manual run.
- **Create the GitHub repo / push** -- see section 3.0. This repo is
  `git init`'d with an initial commit, ready for `git remote add` +
  `git push`.
- **Any Railway configuration itself** -- no `railway` CLI was installed or
  used; section 3 above is the exact manual click-path, including the
  **Root Directory and Start Command changes for both existing services**
  (section 0 and 3.1/3.2) needed for this update's architecture change --
  those must be applied by hand in Railway's dashboard.
- **Namecheap DNS changes** -- section 3.4 above is the exact manual
  click-path.
- **Live-test "Run Pipeline Now" and the Ask page against production
  credentials** -- verified locally with mocked Supabase/pipeline/Claude
  responses (import chain, route behavior, and visual rendering in a
  browser preview, including dark mode) since no live Railway/Supabase/
  Anthropic credentials are available in this environment. Confirm both
  once deployed with real env vars.
