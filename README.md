# Household Bookkeeping System

A self-hosted financial automation system for household and small business bookkeeping, running on a Particle Tachyon device. Uses Flask as the web layer and Claude API for transaction categorization and natural language queries.

Designed for Canadian incorporated businesses filing T2 returns alongside personal finances, with CIBC and RBC CSV formats supported out of the box.

---

## Getting Started

### Prerequisites

- Linux device (tested on Particle Tachyon)
- [Miniforge](https://github.com/conda-forge/miniforge) or Miniconda installed
- Nextcloud sync folder set up and actively syncing
- Anthropic API key (get one at [console.anthropic.com](https://console.anthropic.com))

### Step 1 — Clone the repo

```bash
git clone <repo-url> /home/<username>/Bookkeeping-System
cd /home/<username>/Bookkeeping-System
```

### Step 2 — Create the conda environment

```bash
conda create -n bookkeeping-system-env python=3.11 -y
conda activate bookkeeping-system-env
pip install -r requirements.txt
```

### Step 3 — Create the Nextcloud folder structure

```bash
NEXTCLOUD=/home/<username>/Nextcloud/Bookkeeping-System
mkdir -p "$NEXTCLOUD/bank-transactions/raw"
mkdir -p "$NEXTCLOUD/master/rules"
mkdir -p "$NEXTCLOUD/receipts/raw"
```

The app expects this layout inside your Nextcloud sync folder:

```
Nextcloud/Bookkeeping-System/
├── bank-transactions/
│   ├── raw/                        ← drop raw bank CSVs here
│   ├── personal/YYYY/monthname/    ← auto-created on first run
│   └── business/YYYY/monthname/    ← auto-created on first run
├── master/
│   ├── master_transactions.csv     ← auto-created on first run
│   ├── settings.json               ← auto-created on first run (categories + account types)
│   └── rules/
│       ├── rules.json              ← create manually (see below)
│       └── rules_suggested.json   ← auto-created after each batch run
└── receipts/
    └── raw/                        ← n8n drop zone
```

### Step 4 — Create rules.json

```bash
cat > "$NEXTCLOUD/master/rules/rules.json" << 'EOF'
{
  "version": "1.0",
  "last_updated": "2026-01-01",
  "rules": []
}
EOF
```

Rules are applied before Claude — vendors with rules never incur an API call.

### Step 5 — Configure .env

Create `/home/<username>/Bookkeeping-System/.env`. **Never commit this file.**

```env
# Claude API key — get from console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Password to log in to the dashboard
DASHBOARD_PASSWORD=choose_a_strong_password

# Set to true to bypass login entirely (local dev / debugging only)
AUTO_LOGIN=false

# Telegram bot for rule approvals and reports (can be left blank for now)
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Absolute path to your Nextcloud sync folder
NEXTCLOUD_BASE=/home/<username>/Nextcloud/Bookkeeping-System

# Random string used to sign session cookies
# Generate with: python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY=your_random_secret_here

# Port the Flask app listens on
FLASK_PORT=5000

# Confidence threshold below which Claude-categorized transactions get flagged
CONFIDENCE_THRESHOLD=0.7
```

Other tuneable values (chat history window, top vendor counts, API token limits, rule suggestion threshold, cost estimate per call) are in `config.py` with comments.

### Step 6 — Run the app

```bash
conda activate bookkeeping-system-env
cd /home/<username>/Bookkeeping-System

# Development (foreground, shows logs in terminal)
python app.py

# Production (background, survives terminal close)
nohup python app.py >> bookkeeping.log 2>&1 &
```

Access the dashboard at `http://<device-ip>:5000`.

### Step 7 — (Optional) Run as a systemd service

```ini
# /etc/systemd/system/bookkeeping.service
[Unit]
Description=Bookkeeping System
After=network.target

[Service]
User=<username>
WorkingDirectory=/home/<username>/Bookkeeping-System
ExecStart=/home/<username>/miniforge3/envs/bookkeeping-system-env/bin/python app.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable bookkeeping
sudo systemctl start bookkeeping
```

---

## Importing Bank Transactions

### File naming convention

Raw bank CSVs must follow this naming convention before processing:

```
{bank}-{account_type}-{card_type}[-{alias}]-{YYYYMMDD}-{YYYYMMDD}.csv
```

| Segment | Values | Notes |
|---|---|---|
| `bank` | `cibc`, `rbc`, `td`, `bmo`, `scotiabank`, `hsbc`, `national`, `desjardins` | Lowercase |
| `account_type` | `personal`, `business` | Or any custom type added via Settings |
| `card_type` | `chequing`, `credit`, `savings`, `loc` | `loc` = line of credit |
| `alias` | any lowercase string | Optional — used to distinguish multiple cards of the same type (e.g. `aeroplan`, `dividend`) |
| date range | `YYYYMMDD-YYYYMMDD` | Statement start and end dates |

**Examples:**
```
cibc-personal-chequing-20260101-20260331.csv
cibc-personal-credit-aeroplan-20260101-20260331.csv
cibc-business-chequing-20260101-20260331.csv
rbc-business-credit-20260201-20260331.csv
cibc-personal-loc-20260101-20260331.csv
```

### Smart rename confirmation

If a file in `raw/` does not match the naming convention, the dashboard will pause and show a **File identification** form before processing that file. It:
- Detects bank, account type, card type, and alias from keywords already in the filename
- Shows the first 5 lines of the file as a preview
- Pre-fills all fields — correct anything wrong and confirm
- Previews the new filename live as you type
- Renames the file before processing

Files that already match the convention are processed immediately without pausing.

### Processing files

Drop conforming CSVs into `bank-transactions/raw/` and click **Process new files** in the dashboard (top-right button on any page). A modal shows live progress:

- File X of Y currently processing
- Row-level progress bar: `Row N of M · X new · Y dupes · Z AI calls`
- Completed files listed with per-file stats
- Running API cost in CAD
- Cancel button available at any time

The processor:
1. Checks whether each filename conforms to the naming convention
2. Shows rename confirmation for non-conforming files (pauses until confirmed)
3. Parses the CSV (CIBC and RBC formats auto-detected by bank name)
4. Splits transactions by calendar month
5. Writes monthly organized CSVs to `bank-transactions/{account_type}/YYYY/monthname/`
6. For each transaction: checks rules first (free), falls back to Claude Haiku if no rule matches
7. Appends only new rows to `master_transactions.csv` — duplicates are skipped automatically
8. After all files: generates `rules_suggested.json` from AI-categorized patterns

Safe to re-run at any time. Already-processed transactions are skipped without API calls.

---

## Categories

Categories are stored in `master/settings.json` and are fully configurable via the dashboard (**Rules → Categories**). Changes take effect immediately on the next categorization run.

### Default business categories (Canada T2-aligned)

| Category | Tax note |
|---|---|
| Revenue | Main business income |
| Advertising & Marketing | Deductible expense |
| Banking fees | Deductible expense |
| Charitable donations | Generates donation tax credit on T2 Schedule 2 — not a regular expense |
| Contractor payments | T4A slip required if $500+ paid in the year |
| Government fees & licenses | Deductible |
| Hosting & cloud | Deductible |
| Insurance — business | Deductible |
| Legal & professional services | Deductible |
| Meals & entertainment | 50% deductible — keep separate for year-end |
| Office supplies | Deductible |
| Professional development | Deductible |
| Rent & workspace | Deductible |
| SaaS & software | Deductible |
| Shareholder salary | Salary paid to owner-employee — T4 issued |
| Travel | Deductible |
| Vehicle — fuel | Part of vehicle expense claim (CRA log required) |
| Vehicle — other | Maintenance, insurance, lease — part of vehicle claim |
| Pass-through | Excluded from P&L |
| Credit card payment | Excluded from P&L (inter-account) |
| Uncategorized | Needs review |

### Default personal categories

Income · Remittances · Loan repayment · Rent & housing · Groceries · Dining & takeout · Transport · Healthcare · Insurance — personal · Subscriptions · Utilities · Clothing · Travel · Education · Gifts & donations · Credit card payment · Pass-through · Uncategorized

### Adding or removing categories

Go to **Rules → Categories**, select the account type tab, and use the add/remove controls. Removing a category does not affect existing transactions — they keep the old value in the CSV. To reassign them, run **Re-categorize all** from the Overview page after removing.

---

## Account Types

Account types are configurable via **Rules → Account Types**. Defaults are `personal` and `business`. Removing a type hides its nav tab after a page reload. Adding a new type makes it available in all dropdowns and the categorization prompt immediately.

---

## Rules

Rules are stored in `master/rules/rules.json`. They are checked in order before any Claude API call — first match wins, no further rules are checked.

### Rule schema

```json
{
  "id": "rule-001",
  "description": "GoHighLevel is always SaaS & software",
  "match": {
    "vendor_name_contains": "gohighlevel",
    "account_type": "business",
    "amount_sign": "negative"
  },
  "apply": {
    "category": "SaaS & software",
    "subcategory": "CRM",
    "exclude_from_pnl": false
  }
}
```

**Match fields** (all optional except `vendor_name_contains`):

| Field | Description |
|---|---|
| `vendor_name_contains` | Case-insensitive substring match against the raw bank description |
| `account_type` | `personal` or `business` — omit to match both |
| `amount_sign` | `positive`, `negative`, or omit for either direction |
| `card_type` | `chequing`, `credit`, `savings`, `loc` — omit to match any |

**`amount_sign`** is only needed when the same keyword appears in both income and expense contexts (e.g. a bank description that appears on both sides of a transfer). For most vendors, omit it.

Rules are never overwritten without archiving — a timestamped copy is saved to `master/rules/` automatically before every write.

### Creating rules

Three ways:
1. **AI chat** (Rules page) — describe in plain English, Claude generates the JSON, preview card lets you save or dismiss
2. **Approve a suggestion** — after each batch run, `rules_suggested.json` is populated with vendor patterns Claude identified; one-click approve in the dashboard
3. **Edit modal** — click Edit on any existing rule in the active rules table

### Transfer keywords

Internal transfer keywords are strings that appear in bank descriptions to identify money moving between your own accounts (e.g. `INTERNET TRANSFER`, `PAYMENT THANK YOU`). Configurable via **Rules → Internal transfer keywords**. Used by the pass-through scanner on the Overview page to distinguish:

- **Internal transfers** — chequing → credit card payment (both sides visible in CSV)
- **Pass-throughs** — money received and forwarded to someone else
- **Supplemental income** — real external income that triggered a follow-on internal transfer

---

## Dashboard

Password-protected at `http://<device-ip>:5000`. All views support `?month=YYYY-MM` filtering.

### Overview

Combined KPIs (total in, total out, net), business and personal snapshots, monthly trend bar chart, pass-through scanner, and a WhatsApp-style AI chat panel with full conversation history.

**Pass-through scanner** — scans master_transactions.csv for matching in/out pairs within a configurable dollar tolerance and time window. Marks confirmed pass-throughs as `exclude_from_pnl=True` so they don't distort P&L totals.

**Re-categorize all** — re-runs rules engine + Claude Haiku on every non-manual transaction. Manual overrides (rows where `categorized_by=manual`) are always preserved.

### Business / Personal

Revenue sources and expense breakdown by category as horizontal bar charts, top vendors by spend, full transaction table. Each row shows:
- Date, description, vendor, amount
- Card type pill (chequing / credit / savings / loc) + card alias
- Category, categorization source (rule / AI / manual), confidence score
- Flagged indicator

Inline category editing: click the category on any row to reassign it directly. Optionally create a rule from the edit at the same time.

### Flagged

Transactions Claude categorized with confidence below the threshold (default 0.7). Confidence shown as a colour-coded score (red / amber / green). AI chat available to describe corrections.

### Ledger

Full transaction log across all accounts with search, month filter, and account type filter. Shows card type and alias for every row.

### Rules

Five sections:
1. **Suggested rules** — AI-detected patterns from the last batch run. One-click approve or dismiss.
2. **Active rules** — full rules table with edit and delete per rule.
3. **Internal transfer keywords** — add/remove strings used by the pass-through scanner.
4. **Categories** — tabbed by account type; add or remove categories per type.
5. **Account types** — add or remove account types; controls which nav tabs appear.
6. **AI rule chat** — describe a rule in plain English; Claude generates the JSON.

### AI chat

Available on Overview, Flagged, and Rules pages. Model selector (Haiku default / Sonnet) per chat. Conversation history maintained within the session (configurable window, default 20 turns). Renders markdown. Shows per-message and session token cost in CAD.

**Month-aware queries** — when a month is mentioned ("what's my revenue in January", "last month expenses"), the data summary sent to Claude is scoped to that month automatically.

---

## Routes

| Route | Method | Auth | Description |
|---|---|---|---|
| `/health` | GET | No | Health check — returns `{"status": "ok"}` |
| `/login` | GET/POST | No | Dashboard login |
| `/logout` | GET | No | Clear session |
| `/` or `/dashboard` | GET | Yes | Overview — KPIs, trend chart, pass-through scanner, AI chat |
| `/business` | GET | Yes | Business P&L and transaction table |
| `/personal` | GET | Yes | Personal P&L and transaction table |
| `/ledger` | GET | Yes | Full transaction log with search and filters |
| `/flagged` | GET | Yes | Flagged transactions + AI chat |
| `/rules` | GET | Yes | Rules, suggested rules, keywords, categories, account types |
| `/rules/propose` | POST | Yes | Parse plain-English rule description via Claude |
| `/rules/save` | POST | Yes | Write a proposed rule to rules.json and apply to master CSV |
| `/rules/approve` | POST | Yes | Approve a suggested rule |
| `/rules/dismiss` | POST | Yes | Dismiss a suggested rule |
| `/rules/update` | POST | Yes | Edit an existing rule |
| `/rules/delete` | POST | Yes | Delete a rule |
| `/rules/recategorize` | POST | Yes | Start background re-categorize job |
| `/rules/recategorize/status` | GET | Yes | Poll live progress of re-categorize job |
| `/process/start` | POST | Yes | Start background raw-file processing job |
| `/process/status` | GET | Yes | Poll live progress (phase, row count, file list, totals) |
| `/process/answer` | POST | Yes | Submit file identification answer during waiting_input phase |
| `/process/cancel` | POST | Yes | Cancel a running processing job |
| `/passthrough/scan` | POST | Yes | Scan for pass-through pairs and supplemental income |
| `/passthrough/mark` | POST | Yes | Mark transactions as pass-through (exclude from P&L) |
| `/settings/categories/add` | POST | Yes | Add a category to an account type |
| `/settings/categories/remove` | POST | Yes | Remove a category from an account type |
| `/settings/categories/reorder` | POST | Yes | Replace full category list for one account type |
| `/settings/account-types/add` | POST | Yes | Add a new account type |
| `/settings/account-types/remove` | POST | Yes | Remove an account type |
| `/settings/transfer-keywords` | GET | Yes | Return current transfer keywords |
| `/settings/transfer-keywords/add` | POST | Yes | Add a transfer keyword |
| `/settings/transfer-keywords/delete` | POST | Yes | Remove a transfer keyword |
| `/query` | POST | Yes | NL query — accepts `question`, `model`, `scope` |
| `/query/models` | GET | Yes | Returns available models + default |
| `/ingest/receipt` | POST | No | n8n webhook — receipt ingestion |
| `/ingest/transaction` | POST | No | n8n webhook — bank CSV ingestion |

---

## Key files

| File | Purpose |
|---|---|
| `app.py` | Flask entry point — registers blueprints, auth routes, context processor |
| `config.py` | Paths, API keys, model IDs, default category lists, pricing constants |
| `settings_utils.py` | Load/save `settings.json` — categories and account types configurable at runtime |
| `categorizer.py` | Rules engine + Claude Haiku categorizer |
| `csv_utils.py` | Safe CSV read/append/dedup — all CSV access goes through here |
| `raw_processor.py` | Raw bank CSV pipeline — naming inference, rename confirmation, parse, organize, categorize |
| `recategorize.py` | Re-runs rules + Claude on all non-manual rows in master CSV |
| `dashboard/routes.py` | All dashboard + API routes |
| `dashboard/aggregator.py` | Reads master CSV, computes P&L totals by month/category/account |
| `query/nl.py` | NL query handler — builds data summary, calls Claude, returns answer |
| `ingest/receipts.py` | `/ingest/receipt` endpoint (n8n webhook) |
| `ingest/transactions.py` | `/ingest/transaction` endpoint (n8n webhook) |

---

## Folder structure

```
Bookkeeping-System/              ← This Git repo
├── app.py
├── config.py
├── settings_utils.py
├── categorizer.py
├── csv_utils.py
├── raw_processor.py
├── recategorize.py
├── requirements.txt
├── .env                         ← Secret values (never committed)
├── ingest/
│   ├── receipts.py
│   └── transactions.py
├── dashboard/
│   ├── routes.py
│   └── aggregator.py
├── query/
│   └── nl.py
├── templates/
│   ├── base.html                ← Nav, process modal, theme toggle
│   ├── overview.html
│   ├── business.html
│   ├── personal.html
│   ├── ledger.html
│   ├── flagged.html
│   └── rules.html
└── static/
    ├── style.css
    ├── chat.js
    ├── pagination.js
    └── img/
        ├── logo-light.png
        └── logo-dark.png

Nextcloud/Bookkeeping-System/    ← Data (not in git)
├── bank-transactions/
│   ├── raw/                     ← Drop raw CSVs here
│   ├── personal/YYYY/monthname/
│   └── business/YYYY/monthname/
└── master/
    ├── master_transactions.csv
    ├── master_receipts.csv
    ├── settings.json            ← Categories + account types (UI-editable)
    └── rules/
        ├── rules.json
        ├── rules_suggested.json
        ├── transfer_config.json
        └── rules_YYYY-MM-DD_HHMMSS.json  ← Timestamped archives
```

---

## Architecture

Two layers — this repo owns the Tachyon layer only:

- **n8n** (separate instance) — Telegram ingestion, OCR, email parsing, file saving to Nextcloud, user confirmation flows. Not included in this repo.
- **Tachyon / This repo** — receives webhook calls from n8n, categorizes transactions, writes to master CSVs, and serves the dashboard. `raw_processor.py` is a stand-in until n8n is fully configured for CSV ingestion.

---

## Build phases

- [x] Phase 1 — Project scaffold, config, login, /health
- [x] Phase 2 — CSV utilities (safe read/append, dedup, create-if-missing)
- [x] Phase 3 — Rules engine (load, match, archive before write)
- [x] Phase 4 — Claude Haiku categorizer + rule suggestion after batch
- [ ] Phase 5 — /ingest/transaction endpoint (n8n integration)
- [ ] Phase 6 — /ingest/receipt endpoint (n8n integration)
- [x] Phase 7 — raw_processor.py (stand-in for bank-transactions/raw/)
- [x] Phase 8 — Dashboard aggregator (P&L totals from CSV)
- [x] Phase 9 — Dashboard UI (overview, business, personal, flagged, rules, ledger)
- [x] Phase 10 — NL query (/query endpoint, model-selectable)
- [x] Phase 11 — NL rule creation from chat + rule editing + suggested rule approval
- [ ] Phase 12 — Automated Telegram reports (weekly/monthly P&L summary)

---

## Key rules

1. Never write to `raw/` folders — those are n8n's drop zones
2. Always archive `rules.json` before overwriting — done automatically
3. Never crash on Claude API failure — catch, flag the row, log, continue
4. Always dedup before appending to any master CSV
5. Never hardcode credentials — always read from `.env`
6. Exclude `exclude_from_pnl=True` rows from all P&L totals
7. Manual categorizations (`categorized_by=manual`) are never overwritten by the recategorizer
