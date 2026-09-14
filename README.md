# Financials Pipeline

Automates the Johnson/Suarez household's monthly finance ledger. Two
independent services, one repo:

- **`pipeline/`** -- a run-to-completion script (Railway **Cron Job**, e.g.
  daily). Watches a shared Google Drive folder for new bank/card statements,
  parses them, reconciles the parsed transactions against each statement's
  own printed totals, categorizes every row, and writes everything to
  Supabase. Never guesses: anything it can't confidently parse, reconcile,
  or categorize is left for a human (file stays in Drive; a `needs_review`
  row is queued).
- **`dashboard/`** -- a minimal FastAPI web app (Railway **Web Service**,
  always-on). Password-gated. Three pages: Summary (Flow + Category
  totals), Needs Review (resolve flagged transactions), Assets &
  Liabilities (read-only).

Data lives in Supabase Postgres, project `family-finance`
(`wfpaakmjveuhugskqmup`). This repo does not create or alter that schema.

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
7. Anything matching **no** category is `category='Uncategorized'` with a
   best-effort `flow` guess from the transaction's type. Per statement: if
   the *sum* of that statement's Uncategorized amounts is **>= $50**, every
   Uncategorized row from that statement is queued into `needs_review`
   (this single check subsumes "any single row >= $50", since the sum is
   always >= any individual row -- see `pipeline/categorize.py` docstring).
   **This $50 threshold is adjustable** -- it's `NEEDS_REVIEW_THRESHOLD` in
   `pipeline/categorize.py`.
8. On success: inserts `processed_statements` (`status='processed'`), all
   `transactions` rows (with `statement_id` set), any `needs_review` rows;
   then in Drive, copies the original into **"Raw Originals (Archived)"**
   under its original name, and moves+renames the original (now in root)
   into **"Source Documents (Standardized Names)"**, e.g.
   `"2026-05 - AMEX ConnectMiles (...4473) statement.csv"`.

Run it locally (with a populated `.env`, see below):

```bash
cd pipeline
pip install -r requirements.txt
python main.py
```

## 2. How the dashboard works

Three pages behind a password form (`DASHBOARD_PASSWORD`, checked against a
signed session cookie -- no per-user accounts, per the brief: "keep this
simple, not enterprise-grade"):

- `/summary` -- Flow and Category totals across all transactions.
- `/needs-review` -- open `needs_review` rows, each with a dropdown to
  assign a category (looked up against `category_keys` for the matching
  flow) and mark resolved; writes back to `transactions.category`/`.flow`
  and `needs_review.status='resolved'`.
- `/assets` -- read-only `assets_liabilities`, split into Assets/
  Liabilities with totals and a naive net worth.

Run it locally:

```bash
cd dashboard
pip install -r requirements.txt
uvicorn main:app --reload --port 8000
```

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
   - **Root Directory**: `pipeline`
   - **Build**: leave on Nixpacks (auto-detected from `requirements.txt`
     and `.python-version`); no custom build command needed.
   - **Deploy -> Custom Start Command**: `python main.py`
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
   - **Root Directory**: `dashboard`
   - **Build**: Nixpacks auto-detected, no custom build command.
   - **Deploy -> Custom Start Command**:
     `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Service Type**: leave as the default **Web Service** (always-on;
     this is not a cron job).
3. Set environment variables -- see the table below.
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
| pipeline | `GOOGLE_DRIVE_RAW_ORIGINALS_FOLDER_ID` *(optional)* | Normally left unset -- the pipeline resolves "Raw Originals (Archived)" by name under the root folder automatically and caches it. Set this only if you want to skip that lookup or the folder is ever renamed. |
| dashboard | `SUPABASE_URL` | Same as above. |
| dashboard | `SUPABASE_SERVICE_KEY` | Same `service_role` key as the pipeline (see "why the dashboard prefers the service key" above). Server-side only. |
| dashboard | `SUPABASE_ANON_KEY` *(optional, forward-looking)* | Same API settings page -> **Project API keys -> `anon` `public`**. Currently unused unless `SUPABASE_SERVICE_KEY` is absent (see above); set it anyway so it's ready once real Supabase Auth is added. |
| dashboard | `DASHBOARD_PASSWORD` | Pick your own password for the household. Anyone with it can view and resolve review items. |
| dashboard | `SESSION_SECRET` *(optional)* | Any random string. If omitted, one is derived from `DASHBOARD_PASSWORD`; set this to decouple the cookie-signing key from the login password. |

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
  manual/occasional updates only, shown read-only on `/assets`.

---

## 5. Assumptions made while building this (things the source material left ambiguous)

- **RLS + anon key**: see "why the dashboard prefers the service key"
  above -- a real, load-bearing deviation, not cosmetic.
- **"Raw Originals (Archived)" folder id**: not hardcoded. The read-only
  Drive inspection tool available while building this had a stale/empty
  search index for this specific folder (a known lag issue, not a real
  absence of the folder). Rather than guess at an id, `drive_client.py`
  resolves it **by name** under the root folder at runtime and caches it --
  arguably more robust than a hardcoded id anyway. Override with
  `GOOGLE_DRIVE_RAW_ORIGINALS_FOLDER_ID` if you ever want to skip the
  lookup.
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

## 6. What I could NOT do from here (needs you)

- **Live-test against a real Drive service account key** -- I don't have
  the key (correctly -- it should never be pasted into a chat). Test with a
  real statement drop after deploying, and watch the pipeline service's
  logs on its first scheduled/manual run.
- **Create the GitHub repo / push** -- see section 3.0. This repo is
  `git init`'d with an initial commit, ready for `git remote add` +
  `git push`.
- **Any Railway configuration itself** -- no `railway` CLI was installed or
  used; section 3 above is the exact manual click-path.
- **Namecheap DNS changes** -- section 3.4 above is the exact manual
  click-path.
