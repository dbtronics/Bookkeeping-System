# Household Bookkeeping System

A self-hosted financial automation system for household and business bookkeeping, running on a Particle Tachyon device. Uses Flask as the web layer and Claude API for transaction categorization and natural language queries.

---

## Getting Started

Follow these steps to get the system running from scratch on a new device.

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
conda create -n bookkeeping-system-env python -y
conda activate bookkeeping-system-env
pip install -r requirements.txt
```

### Step 3 — Create the Nextcloud folder structure

The app expects this layout inside your Nextcloud sync folder:

```
Nextcloud/Bookkeeping-System/
├── bank-transactions/
│   └── raw/           ← drop raw bank CSVs here
├── master/
│   └── rules/         ← rules.json lives here
└── receipts/
    └── raw/           ← n8n drop zone
```

Create it manually if it doesn't exist:

```bash
NEXTCLOUD=/home/<username>/Nextcloud/Bookkeeping-System
mkdir -p "$NEXTCLOUD/bank-transactions/raw"
mkdir -p "$NEXTCLOUD/master/rules"
mkdir -p "$NEXTCLOUD/receipts/raw"
```

### Step 4 — Create rules.json

Create an initial empty rules file at `$NEXTCLOUD/master/rules/rules.json`:

```json
{
  "version": "1.0",
  "last_updated": "2026-01-01",
  "rules": []
}
```

Rules are applied before Claude AI — any vendor you add here won't incur an API call.

### Step 5 — Configure .env

Create `/home/<username>/Bookkeeping-System/.env`. **Never commit this file.**

```env
# Claude API key — get from console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Password to log in to the dashboard
DASHBOARD_PASSWORD=choose_a_strong_password

# Set to true to bypass login entirely (useful for local dev/debugging)
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

# Set to true to bypass login entirely (useful for local dev/debugging)
AUTO_LOGIN=false
```

All other tuneable values (chat history window, top vendor count, API token limits, rule suggestion threshold, cost estimate) are in `config.py` with comments indicating which file each variable affects.

### Step 6 — Import your bank transactions

Name raw CSV exports using this convention and drop them into `bank-transactions/raw/`:

| Filename | Bank | Account |
|---|---|---|
| `cibc-business-cc.csv` | CIBC | Business credit card |
| `cibc-business-dc.csv` | CIBC | Business chequing/debit |
| `cibc-personal-cc.csv` | CIBC | Personal credit card |
| `cibc-personal-dc.csv` | CIBC | Personal chequing/debit |
| `cibc-personal-loc.csv` | CIBC | Personal line of credit |
| `rbc-business-cc.csv` | RBC | Business credit card |
| `rbc-business-dc.csv` | RBC | Business chequing/debit |

Then run:

```bash
conda activate bookkeeping-system-env
cd /home/<username>/Bookkeeping-System
python raw_processor.py
```

This will organise files by month, categorise every transaction (rules first, Claude Haiku fallback), and populate `master_transactions.csv`. Check `bookkeeping.log` for details.

### Step 7 — Run the app

```bash
conda activate bookkeeping-system-env
cd /home/<username>/Bookkeeping-System

# Development (foreground)
python app.py

# Production (background, survives terminal close)
nohup python app.py >> bookkeeping.log 2>&1 &
```

Access the dashboard at `http://<device-ip>:5000` from any browser on your network.

### Step 8 — (Optional) Run as a systemd service

To have the app start automatically on boot:

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

## Architecture

This system has two layers:

- **n8n** (separate instance) — handles Telegram ingestion, OCR, email parsing, file saving to Nextcloud, and user confirmation flows. This repo does not include n8n workflows.
- **Tachyon / This repo** — receives webhook calls from n8n, categorizes transactions, writes to master CSVs, and serves the dashboard.

---

## Key files and what they do

### `raw_processor.py`
The main data pipeline. Run this whenever you have new bank exports to process. It does everything in sequence:
1. Scans `bank-transactions/raw/` for CSV files
2. Parses each file (CIBC and RBC formats supported)
3. Splits transactions by month and writes organised CSVs to `bank-transactions/business/` or `bank-transactions/personal/`
4. For each transaction, runs it through the rules engine first (free, instant) — if no rule matches, calls Claude Haiku to categorize it
5. Appends only new rows to `master_transactions.csv` — duplicate detection is automatic
6. Writes `rules_suggested.json` with vendor patterns Claude identified for one-click rule approval in the dashboard

Safe to run any time. Already-processed rows are skipped instantly (no API calls for duplicates). Logs everything to `bookkeeping.log`.

### `app.py`
The web dashboard. Reads `master_transactions.csv` on every page load and serves the UI. It does not process any raw files — it only reads what `raw_processor.py` has already written.

Serves five views: Overview (combined P&L + trend chart + AI chat), Business, Personal, Flagged transactions, and Rules management. Also exposes the `/query` endpoint for natural language questions answered by Claude.

Run with `./run.sh` — this runs `raw_processor.py` first, then starts the dashboard.

---

## Folder structure

```
Bookkeeping-System/          ← This Git repo (project code)
├── app.py                   ← Flask entry point
├── config.py                ← Paths, API keys, category lists, NL model config
├── categorizer.py           ← Rules engine + Claude Haiku categorizer
├── csv_utils.py             ← Safe CSV read/append/dedup utilities
├── raw_processor.py         ← TEMPORARY: organises raw bank CSVs until n8n is ready
├── requirements.txt
├── .env                     ← Secret values (never committed)
├── ingest/
│   ├── receipts.py          ← POST /ingest/receipt
│   └── transactions.py      ← POST /ingest/transaction
├── dashboard/
│   ├── routes.py            ← All dashboard routes + /rules API
│   └── aggregator.py        ← CSV reader, P&L totals by month/category/account
├── query/
│   └── nl.py                ← POST /query — NL handler, builds summary, calls Claude
├── templates/               ← Jinja2 HTML templates (base, overview, business, personal, flagged, rules)
└── static/
    ├── style.css            ← Full design system (light + dark theme via CSS variables)
    ├── chat.js              ← Reusable WhatsApp-style chat widget
    └── img/
        ├── logo-light.png   ← Logo for light theme
        └── logo-dark.png    ← Logo for dark theme

Nextcloud/Bookkeeping-System/   ← Nextcloud sync folder (data lives here, not in git)
├── bank-transactions/
│   ├── raw/                 ← Drop raw bank CSVs here
│   ├── personal/YYYY/monthname/
│   └── business/YYYY/monthname/
└── master/
    ├── master_transactions.csv
    ├── master_receipts.csv
    └── rules/
        ├── rules.json
        └── rules_suggested.json   ← AI-detected patterns awaiting approval
```

---

## Dashboard

Password-protected at `http://<device-ip>:5000`. All views support `?month=YYYY-MM` filtering.

**Overview** — Combined KPIs (total in, total out, net), business and personal snapshots, monthly trend bar chart, and a WhatsApp-style AI chat panel.

**Business** — Revenue sources and expense breakdown by category as horizontal bar charts, top vendors by spend, full transaction table with categorization source (rule / AI / manual) and flagged indicators.

**Personal** — Same layout as Business but for personal accounts.

**Flagged** — Transactions Claude categorized with confidence below 0.7. Shows confidence score colour-coded (red / amber / green), flag reason, and an AI chat to describe corrections.

**Rules** — Three sections:
1. *Suggested rules* — patterns Claude identified after each batch run. One-click approve or dismiss.
2. *NL rule editor* — describe a rule in plain English via chat; Claude responds with what it would create.
3. *Active rules table* — read-only view of all rules in priority order.

**AI chat** — Available on Overview, Flagged, and Rules pages. Model selector (Haiku default / Sonnet) on each chat. Enter to send, conversation history maintained within the session (up to 20 turns). Renders markdown (bold, lists, headings).

The data summary sent to Claude includes: monthly P&L, category breakdown, top vendors, all excluded-from-P&L transactions, all flagged transactions.

**Month-aware filtering** — when a month is mentioned in a question ("what's my revenue in January", "last month expenses"), the entire summary is automatically scoped to that month only — totals, categories, and vendor breakdowns all filter accordingly. When no year is stated, the most recent year in the data is assumed and Claude will say so explicitly so you can correct it. Supported formats: month names ("january", "jan"), "this month", "last month", and explicit year ("january 2025").

**Design:** DM Sans + DM Mono fonts, cream / dark background, teal for business, purple for personal. Theme toggle (light/dark) in sidebar + mobile top bar. Mobile-responsive — sidebar collapses to a slide-in drawer on screens ≤ 768px.

---

## Routes

| Route | Method | Auth | Description |
|---|---|---|---|
| `/health` | GET | No | Health check |
| `/login` | GET/POST | No | Dashboard login |
| `/logout` | GET | No | Clear session |
| `/` or `/dashboard` | GET | Yes | Overview — KPIs, trend chart, AI chat |
| `/business` | GET | Yes | Business P&L, transaction table |
| `/personal` | GET | Yes | Personal P&L, transaction table |
| `/flagged` | GET | Yes | Flagged transactions + AI chat |
| `/rules` | GET | Yes | Rules management + AI chat |
| `/rules/approve` | POST | Yes | Approve a suggested rule |
| `/rules/dismiss` | POST | Yes | Dismiss a suggested rule |
| `/query` | POST | Yes | NL query — accepts `question`, `model`, `scope` |
| `/query/models` | GET | Yes | Returns available models + default |
| `/ingest/receipt` | POST | No | n8n webhook — receipt ingestion |
| `/ingest/transaction` | POST | No | n8n webhook — bank CSV ingestion |
| `/rules/propose` | POST | Yes | Propose a rule (pending Telegram confirmation) |
| `/rules/confirm` | POST | No | Telegram webhook — write confirmed rule |

---

## What's left to build

### Phase 11 — Rule proposal via Telegram
`/rules/propose` and `/rules/confirm` are stubbed. When a user describes a rule in the NL chat, the intent is to:
1. Have Claude generate the rule JSON
2. Send a Telegram message: *"New rule proposed: [description]. Reply YES to apply."*
3. Save it to `rules_pending.json`
4. On `/rules/confirm` (Telegram webhook), write it to `rules.json`

Currently the chat on the Rules page answers in text only — no actual write happens.

### Phase 12 — Automated Telegram reports
Weekly and monthly P&L summaries sent to the owner's Telegram chat automatically. Planned as a cron job calling a Flask endpoint or a standalone script.

### Inline transaction editing
The transaction tables on Business/Personal/Flagged are read-only. A future edit flow would allow clicking a row to correct its category, vendor name, or P&L exclusion status directly. Currently corrections must be made by editing `master_transactions.csv` directly or by writing a rule.

### Receipts
`master_receipts.csv` and the `/ingest/receipt` endpoint exist but the receipts dashboard view is not built. Receipts ingestion (via n8n OCR) is a separate workstream.

### n8n integration
`raw_processor.py` is a temporary stand-in. Once n8n is configured, it will call `/ingest/transaction` directly after moving a CSV to the structured folder. `raw_processor.py` should then be disabled.

### Bank support
`raw_processor.py` currently supports CIBC and RBC CSV formats. Additional banks require adding a parser function to `FILE_CONFIGS` in `raw_processor.py`.

---

## Build phases

- [x] Phase 1 — Project scaffold, config, login, /health
- [x] Phase 2 — CSV utilities (safe read/append, dedup, create-if-missing)
- [x] Phase 3 — Rules engine (load rules.json, match, archive before write)
- [x] Phase 4 — Claude Haiku categorizer + rule suggestion after batch
- [ ] Phase 5 — /ingest/transaction endpoint (stubbed — returns 501, built by n8n integration)
- [ ] Phase 6 — /ingest/receipt endpoint (stubbed — returns 501, built by n8n integration)
- [x] Phase 7 — raw_processor.py (cron fallback for bank-transactions/raw/)
- [x] Phase 8 — Dashboard aggregator (P&L totals from CSV by month/category/account)
- [x] Phase 9 — Dashboard UI (overview, business, personal, flagged, rules — mobile-responsive, dark/light theme, WhatsApp-style AI chat)
- [x] Phase 10 — NL query (/query endpoint, model-selectable — Haiku default)
- [ ] Phase 11 — Rule proposal flow (/rules/propose, /rules/confirm, Telegram)
- [ ] Phase 12 — Automated reports (weekly/monthly Telegram summary)

---

## Key rules

1. Never write to `raw/` folders — those are n8n's drop zones
2. Always archive `rules.json` before overwriting — timestamp copy is saved automatically
3. Never crash on Claude API failure — catch, flag the row, log, continue
4. Always dedup — check before appending to any master CSV
5. Never hardcode credentials — always read from `.env`
6. Exclude `exclude_from_pnl: true` rows from all P&L totals (inter-account transfers, owner draws, CC payments)
