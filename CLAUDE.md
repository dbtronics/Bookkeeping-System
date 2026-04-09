# Household Bookkeeping System — Tachyon/Particle Device

## What you are building

A self-hosted financial automation system for a household bookkeeping setup. You are working exclusively on the **Particle Tachyon layer**. A separate n8n instance handles the ingestion pipeline (Telegram, email, OCR, Telegram confirmation loops, file saving to Nextcloud). Your work starts after n8n has finished its job.

Do not build n8n workflows. Do not build Telegram bots. Your boundary is defined clearly below.

---

## Your exact responsibility

You own two things that share one Python codebase on this device:

**1. Ingest service (Flask endpoints)**
Webhook receivers that n8n calls after it has saved files to Nextcloud. These endpoints read the saved JSON sidecar, apply categorization logic, and write results to the master CSV files.

**2. Dashboard + NL query app (Flask routes)**
A password-protected web app that reads the master CSV files and renders the dashboard. Also handles natural language queries by calling the Claude API.

Both live in the same Flask app. Same codebase. Same Git repo.

---

## Device and environment

- **Device**: Particle Tachyon (Linux, self-hosted)
- **Language**: Python 3 only
- **Framework**: Flask (keep it simple and readable)
- **Version control**: Git — every meaningful change must be committed
- **Conda environment name**: `bookkeeping-system-env`
- **No external database** — CSV files are the source of truth for now
- **Claude API model for categorization**: `claude-haiku-4-5-20251001`
- **Claude API model for NL queries**: `claude-sonnet-4-6`

**Path variables — replace with actual values before starting:**

| Variable | Description | Example |
|---|---|---|
| `[USERNAME]` | Linux username on this device | `particle` |
| `[PROJECT_PATH]` | Absolute path to the Git project folder | `/home/particle/Bookkeeping-System` |
| `[NEXTCLOUD_PATH]` | Absolute path to Nextcloud sync folder | `/home/particle/Nextcloud/Bookkeeping-System` |

The project folder and the Nextcloud folder are **separate**. The project lives in `[PROJECT_PATH]` and is a Git repo. Nextcloud at `[NEXTCLOUD_PATH]` is where files are stored — the project reads and writes there but does not live there.

---

## Nextcloud folder structure (read-only reference)

```
[NEXTCLOUD_PATH]/
├── receipts/
│   ├── raw/                          ← n8n drop zone (do not touch)
│   └── YYYY/
│       └── monthname/
│           ├── vendor_YYYY-MM-DD_HHMMSS.pdf   ← receipt file
│           └── vendor_YYYY-MM-DD_HHMMSS.json  ← metadata sidecar
├── bank-transactions/
│   ├── raw/                          ← n8n drop zone (do not touch)
│   ├── personal/
│   │   └── YYYY/
│   │       └── monthname/
│   │           ├── bankname_personal_mmmYYYY-mmmYYYY.csv
│   │           └── bankname_personal_mmmYYYY-mmmYYYY.json
│   └── business/
│       └── YYYY/
│           └── monthname/
│               ├── bankname_business_mmmYYYY-mmmYYYY.csv
│               └── bankname_business_mmmYYYY-mmmYYYY.json
└── master/
    ├── master_transactions.csv
    ├── master_receipts.csv
    └── rules/
        ├── rules.json
        └── rules_YYYY-MM-DD_HHMMSS.json   ← archived copies
```

Never write to the `raw/` folders. Never delete from `receipts/` or `bank-transactions/`. Only write to `master/`.

---

## Master CSV schemas

### master_transactions.csv

All amounts in CAD. One row per bank transaction.

| Field | Type | Notes |
|---|---|---|
| transaction_id | string | Format: TXN-YYYYMMDD-NNNN |
| source_file | string | Filename of raw CSV it came from |
| import_date | date | YYYY-MM-DD — when Tachyon processed it |
| date | date | YYYY-MM-DD — from bank statement |
| description | string | Raw bank description, never modified |
| vendor_name | string | Cleaned name from Claude API or rules |
| amount | number | Negative = expense, positive = income, CAD |
| bank_name | string | e.g. CIBC |
| account_type | enum | personal · business |
| card_type | enum | chequing · savings · credit · debit |
| category | string | Top-level e.g. SaaS tools · Remittances |
| subcategory | string | Optional |
| categorized_by | enum | rule · ai · manual |
| confidence | number | 0.0–1.0, null if rule/manual |
| flagged | boolean | True if needs review |
| flag_reason | string | Optional |
| exclude_from_pnl | boolean | True for inter-account transfers, lump sums |
| notes | string | Optional free text |

### master_receipts.csv

One row per filed receipt. Built by scanning all JSON sidecar files.

| Field | Type | Notes |
|---|---|---|
| receipt_id | string | Format: RCP-YYYYMMDD-HHMMSS |
| source_file | string | Full path to receipt file |
| metadata_file | string | Full path to JSON sidecar |
| file_type | enum | pdf · png · jpg · jpeg · other |
| ingested_via | enum | email · telegram · slack · sms · manual |
| ingested_date | date | YYYY-MM-DD |
| receipt_date | date | YYYY-MM-DD — date on the receipt |
| vendor_name | string | From OCR |
| subtotal | number | Optional |
| tax | number | Optional |
| total_amount | number | Required |
| amount_cad | number | Optional — only if foreign currency |
| currency | string | 3-letter code, default CAD |
| line_items | string | JSON-encoded array |
| account_type | enum | personal · business |
| category | string | Optional |
| flagged | boolean | |
| ocr_confidence | number | 0.0–1.0 |
| notes | string | Optional |

---

## rules.json schema

```json
{
  "version": "1.0",
  "last_updated": "YYYY-MM-DD",
  "rules": [
    {
      "id": "rule-001",
      "description": "Human readable description",
      "match": {
        "vendor_name_contains": "GoHighLevel",
        "account_type": "business"
      },
      "apply": {
        "category": "SaaS tools",
        "subcategory": "CRM",
        "exclude_from_pnl": false
      }
    }
  ]
}
```

Rules are checked before any Claude API call. If a rule matches, Claude is not called. The rules engine must check rules in order and stop at the first match.

**Before every rules.json write**: archive the current version as `rules_YYYY-MM-DD_HHMMSS.json` in the same folder. Never overwrite without archiving first.

---

## Flask app structure

```
[PROJECT_PATH]/
├── CLAUDE.md               ← this file (Claude Code reads it automatically)
├── .env                    ← environment variables (never commit this)
├── .gitignore
├── requirements.txt        ← pip dependencies for conda env
├── app.py                  ← Flask app entry point
├── config.py               ← paths, API keys, thresholds
├── categorizer.py          ← rules engine + Claude API categorization
├── watcher.py              ← standalone folder watcher / cron fallback
├── ingest/
│   ├── __init__.py
│   ├── receipts.py         ← /ingest/receipt endpoint
│   └── transactions.py     ← /ingest/transaction endpoint
├── dashboard/
│   ├── __init__.py
│   ├── routes.py           ← /dashboard, /business, /personal, /receipts
│   └── aggregator.py       ← reads CSVs, computes P&L totals
├── query/
│   ├── __init__.py
│   └── nl.py               ← /query endpoint, Claude API NL handler
├── templates/
│   ├── base.html
│   ├── overview.html
│   ├── business.html
│   ├── personal.html
│   └── receipts.html
└── static/
    └── style.css
```

---

## Ingest endpoints

### POST /ingest/receipt

Called by n8n after it has saved a receipt file and its JSON sidecar to Nextcloud.

**Request body:**
```json
{
  "metadata_file": "[NEXTCLOUD_PATH]/receipts/2025/october/loom_2025-10-03_105645345.json"
}
```

**What this endpoint does:**
1. Read the JSON sidecar at `metadata_file`
2. Apply rules.json to assign a category (if no rule matches, skip — category stays null for receipts)
3. Append one new row to `master_receipts.csv` (check receipt_id for dedup first)
4. Return `{ "status": "ok", "receipt_id": "RCP-..." }`

### POST /ingest/transaction

Called by n8n after it has confirmed a bank CSV upload and moved it to the correct folder.

**Request body:**
```json
{
  "csv_file": "[NEXTCLOUD_PATH]/bank-transactions/business/2025/october/cibc_business_oct2025-oct2025.csv",
  "metadata_file": "[NEXTCLOUD_PATH]/bank-transactions/business/2025/october/cibc_business_oct2025-oct2025.json",
  "account_type": "business",
  "bank_name": "CIBC"
}
```

**What this endpoint does:**
1. Read the CSV file row by row
2. For each row: generate a transaction_id, check if it already exists in master_transactions.csv (dedup by date + description + amount)
3. For new rows: run through rules.json first. If no match, call Claude Haiku API with the transaction description to get category + confidence
4. Append only new rows to master_transactions.csv
5. Return `{ "status": "ok", "added": N, "duplicates": N, "flagged": N }`

---

## Categorization logic (categorizer.py)

```python
def categorize_transaction(row, rules):
    # 1. Try rules first
    for rule in rules:
        if matches_rule(row, rule):
            return {
                "category": rule["apply"]["category"],
                "subcategory": rule["apply"].get("subcategory"),
                "categorized_by": "rule",
                "confidence": None,
                "exclude_from_pnl": rule["apply"].get("exclude_from_pnl", False),
                "flagged": False,
                "flag_reason": None
            }
    
    # 2. Fall back to Claude Haiku
    result = call_claude_categorize(row)
    flagged = result["confidence"] < 0.7
    return {
        "category": result["category"],
        "subcategory": result.get("subcategory"),
        "categorized_by": "ai",
        "confidence": result["confidence"],
        "exclude_from_pnl": False,
        "flagged": flagged,
        "flag_reason": "low confidence" if flagged else None
    }
```

Claude Haiku prompt for categorization must include:
- The transaction description (raw)
- The vendor name (cleaned)
- The amount and account type
- A hardcoded list of valid categories from config.py
- Instruction to return ONLY a JSON object: `{ "category": "...", "subcategory": "...", "confidence": 0.0-1.0 }`

---

## Dashboard routes

All routes read from CSV on each request. No caching initially.

| Route | View | Data source |
|---|---|---|
| `/` or `/dashboard` | Overview | master_transactions.csv — both |
| `/business` | Business | master_transactions.csv — account_type=business |
| `/personal` | Personal | master_transactions.csv — account_type=personal |
| `/receipts` | Receipts | master_receipts.csv |
| `/query` (POST) | NL query | master_transactions.csv summary + Claude Sonnet |

Month filter is a query param: `?month=2025-10`

All views exclude rows where `exclude_from_pnl=True` from revenue/expense totals.

---

## NL query (query/nl.py)

**POST /query**

Request: `{ "question": "What did I spend on SaaS last month?", "scope": "all" }`

Scope options: `all` · `business` · `personal` · `receipts`

Steps:
1. Read master_transactions.csv (filtered by scope)
2. Build a compact summary: list of categories with totals, top vendors, date range
3. Call Claude Sonnet with: the question + the compact summary + instructions to answer in plain English
4. If the answer implies a new exception rule, append a `suggested_rule` field to the response
5. Return `{ "answer": "...", "suggested_rule": null | { rule object } }`

The front end displays the answer and, if `suggested_rule` is present, shows a "Create rule?" prompt. On confirmation, the front end calls `POST /rules/propose` which sends a Telegram message to the owner for approval before writing to rules.json.

---

## Exception rule proposal flow

**POST /rules/propose**

1. Receive proposed rule JSON from front end
2. Archive current rules.json with timestamp
3. Send Telegram message: "New rule proposed: [description]. Reply YES to apply."
4. Save the proposed rule to a temporary `rules_pending.json`
5. Return `{ "status": "pending" }`

**POST /rules/confirm** (called by Telegram webhook or manually)

1. Read rules_pending.json
2. Load current rules.json
3. Append the new rule
4. Write rules.json
5. Clean up rules_pending.json
6. Send Telegram confirmation: "Rule applied."

---

## Polling for new bank CSVs (background task)

Since n8n handles the ingestion trigger for receipts, the question is how bank CSVs get processed on the Tachyon side. Two approaches — implement whichever fits the setup:

**Option A — n8n webhook (preferred)**
n8n detects the new CSV in the raw folder, confirms with the user via Telegram, moves it to the right folder, then POSTs to `/ingest/transaction`. No polling needed on Tachyon.

**Option B — folder watcher (fallback)**
If n8n is not yet set up for CSV ingestion, Tachyon can poll using Python's `watchdog` library or a simple cron job:

```python
# Simple cron approach — runs every 5 minutes
import glob, os

# RAW_PATHS is read from config.py which reads from .env
RAW_PATHS = [
    os.path.join(os.environ.get("NEXTCLOUD_BASE"), "bank-transactions/raw/")
]

def check_raw_folders():
    for path in RAW_PATHS:
        for f in glob.glob(path + "*.csv"):
            # Process file
            # Move to correct year/month folder after processing
            pass
```

Build Option B as a standalone script `watcher.py` so it can be run as a cron job or systemd service independently of the Flask app. It should be easy to disable once n8n takes over.

To run watcher.py on a schedule via cron (example — every 5 minutes):
```
*/5 * * * * /home/[USERNAME]/miniconda3/envs/bookkeeping-system-env/bin/python [PROJECT_PATH]/watcher.py >> [PROJECT_PATH]/bookkeeping.log 2>&1
```

---

## Authentication

Simple single-user password auth using Flask session. No user database needed.

```python
PASSWORD = os.environ.get("DASHBOARD_PASSWORD")  # set in .env
```

Login route: `GET/POST /login`
Logout route: `GET /logout`
All dashboard and query routes require `@login_required` decorator.

---

## Environment variables (.env file)

```
ANTHROPIC_API_KEY=your_key_here
DASHBOARD_PASSWORD=your_password_here
TELEGRAM_BOT_TOKEN=your_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
NEXTCLOUD_BASE=[NEXTCLOUD_PATH]
FLASK_SECRET_KEY=random_secret_here
FLASK_PORT=5000
CONFIDENCE_THRESHOLD=0.7
```

Replace `[NEXTCLOUD_PATH]` with the actual absolute path before running.
Never hardcode any of these values. Always read from environment.

---

## Categories (config.py)

These are the valid top-level categories for transactions. Claude must only return one of these:

**Business:**
- Revenue
- SaaS tools
- Contractors
- Insurance
- Hosting
- Advertising
- Banking fees
- Office supplies
- Professional services
- Uncategorized

**Personal:**
- Income
- Remittances
- Loan repayment
- Living expenses
- Insurance
- Subscriptions
- Healthcare
- Transport
- Uncategorized

---

## Coding standards

- Keep every file under 200 lines. Split into modules if it grows beyond that.
- Write plain, readable Python. No over-engineering. No unnecessary abstractions.
- Every function that touches a CSV must handle the case where the file does not exist yet (create with headers on first run).
- Every function that calls Claude API must have a try/except. On failure, flag the row and continue — never crash.
- Log everything to a simple `bookkeeping.log` file. Use Python's built-in `logging` module.
- No async. No background threads in Flask. Keep it synchronous and simple.
- Commit after every working feature. Commit message format: `[phase] short description` e.g. `[ingest] add transaction dedup logic`

---

## Build order

Build in this order. Do not skip phases. Do not build phase N+1 until phase N is working and committed.

- [ ] **Phase 1** — Project scaffold: folder structure, config.py, .env loading, Flask app skeleton, login/logout
- [ ] **Phase 2** — CSV utilities: functions to read/append master_transactions.csv and master_receipts.csv safely (create if missing, dedup check, append row)
- [ ] **Phase 3** — Rules engine: load rules.json, match a transaction against rules, archive before write
- [ ] **Phase 4** — Claude categorizer: call Haiku API with transaction data, parse JSON response, handle errors
- [ ] **Phase 5** — /ingest/transaction endpoint: full pipeline from CSV file → categorize → append to master CSV
- [ ] **Phase 6** — /ingest/receipt endpoint: read JSON sidecar → optional category → append to master_receipts.csv
- [ ] **Phase 7** — watcher.py: standalone folder watcher / cron script for bank-transactions/raw/ as fallback
- [ ] **Phase 8** — Dashboard aggregator: read master_transactions.csv, compute P&L by category/account_type/month
- [ ] **Phase 9** — Flask dashboard routes + HTML templates: overview, business, personal, receipts views
- [ ] **Phase 10** — NL query: /query endpoint, Claude Sonnet call, suggested rule extraction
- [ ] **Phase 11** — Rule proposal flow: /rules/propose, /rules/confirm, Telegram notification
- [ ] **Phase 12** — Automated reports: weekly and monthly Telegram summary, upload reminder

---

## How to start (Phase 1)

1. Confirm conda is installed on this device: `conda --version`
2. Create the conda environment: `conda create -n bookkeeping-system-env python=3.11 -y`
3. Activate it: `conda activate bookkeeping-system-env`
4. Create the project directory at `[PROJECT_PATH]` and cd into it
5. Initialise a Git repo: `git init`
6. Install initial dependencies: `pip install flask python-dotenv anthropic requests`
7. Save dependencies: `pip freeze > requirements.txt`
8. Create `.env` with all required variables (fill in actual values)
9. Create `.gitignore` — must include `.env`, `*.log`, `__pycache__/`, `*.pyc`
10. Scaffold the folder structure shown above
11. Write `app.py` with Flask app init, login/logout, and a `/health` route that returns `{ "status": "ok" }`
12. Commit: `git add -A && git commit -m "[phase1] initial scaffold"`
13. Run the app: `flask run --port 5000`
14. Confirm `http://localhost:5000/health` returns `{ "status": "ok" }` before moving to Phase 2

When Phase 1 is confirmed working, ask before starting Phase 2.

To activate the environment in future sessions:
```bash
conda activate bookkeeping-system-env
cd [PROJECT_PATH]
```

---

## Key rules to never break

1. **Never write to raw/ folders** — those are n8n's drop zones
2. **Never overwrite rules.json without archiving** — always timestamp-archive first
3. **Never crash on Claude API failure** — catch, flag, log, continue
4. **Never double-count inter-account transfers** — exclude_from_pnl=True rows are excluded from all P&L totals
5. **Never hardcode credentials** — always from .env
6. **Always dedup** — check transaction_id or (date + description + amount) before appending to master CSV
7. **Keep it simple** — if something feels over-engineered, it probably is. Ask.
