# Household Bookkeeping System

A self-hosted financial automation system for household and business bookkeeping, running on a Particle Tachyon device. Uses Flask as the web layer and Claude API for transaction categorization and natural language queries.

---

## Architecture

This system has two layers:

- **n8n** (separate instance) — handles Telegram ingestion, OCR, email parsing, file saving to Nextcloud, and user confirmation flows. This repo does not include n8n workflows.
- **Tachyon / This repo** — receives webhook calls from n8n, categorizes transactions, writes to master CSVs, and serves the dashboard.

---

## What this app does

1. **Ingest endpoints** — Flask webhooks that n8n calls after saving files to Nextcloud. Reads JSON sidecars, applies categorization rules, writes to master CSV files.
2. **Dashboard** — Password-protected web app showing P&L by category, account type, and month.
3. **Natural language queries** — Ask questions in plain English; Claude Sonnet answers using the master CSV as context.
4. **Rule proposal flow** — Suggest new categorization rules from the dashboard; approve via Telegram before writing.

---

## Folder structure

```
Bookkeeping-System/          ← This Git repo (project code)
├── app.py                   ← Flask entry point
├── config.py                ← Paths, API keys, category lists
├── categorizer.py           ← Rules engine + Claude Haiku categorizer
├── raw_processor.py         ← TEMPORARY: organizes raw bank CSVs until n8n is ready
├── watcher.py               ← Standalone folder watcher / cron fallback
├── requirements.txt
├── .env                     ← Secret values (never committed)
├── ingest/
│   ├── receipts.py          ← POST /ingest/receipt
│   └── transactions.py      ← POST /ingest/transaction
├── dashboard/
│   ├── routes.py            ← /dashboard, /business, /personal, /receipts
│   └── aggregator.py        ← CSV reader, P&L totals
├── query/
│   └── nl.py                ← POST /query (Claude Sonnet NL handler)
├── templates/               ← Jinja2 HTML templates
└── static/                  ← CSS

Nextcloud/Bookkeeping-System/   ← Nextcloud sync folder (data lives here)
├── receipts/
│   ├── raw/                 ← n8n drop zone (never write here)
│   └── YYYY/monthname/
├── bank-transactions/
│   ├── raw/                 ← Drop raw bank CSVs here for raw_processor.py
│   ├── personal/YYYY/monthname/
│   └── business/YYYY/monthname/
└── master/
    ├── master_transactions.csv
    ├── master_receipts.csv
    └── rules/
        └── rules.json
```

---

## Environment setup

### 1. Create conda environment

```bash
conda create -n bookkeeping-system-env python -y
conda activate bookkeeping-system-env
pip install -r requirements.txt
```

### 2. Configure .env

Create a `.env` file in the project root. **Never commit this file.**

```env
# Claude API key — get from console.anthropic.com
ANTHROPIC_API_KEY=sk-ant-...

# Password to log in to the dashboard
DASHBOARD_PASSWORD=choose_a_strong_password

# Telegram bot for rule approvals and reports
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# Absolute path to your Nextcloud sync folder
NEXTCLOUD_BASE=/home/particle/Nextcloud/Bookkeeping-System

# Random string used to sign session cookies — generate with: python -c "import secrets; print(secrets.token_hex(32))"
FLASK_SECRET_KEY=your_random_secret_here

# Port the Flask app listens on
FLASK_PORT=5000

# Confidence threshold below which Claude-categorized transactions get flagged for review
CONFIDENCE_THRESHOLD=0.7
```

### 3. Run the app

```bash
conda activate bookkeeping-system-env
cd /home/particle/Bookkeeping-System

# Foreground (development)
flask --app app run --host 0.0.0.0 --port 5000

# Background (persistent)
nohup flask --app app run --host 0.0.0.0 --port 5000 >> bookkeeping.log 2>&1 &
```

Access at `http://<device-ip>:5000`

---

## Temporary: processing raw bank CSVs

Until n8n is configured to handle bank CSV ingestion, use `raw_processor.py` to manually process raw exports:

1. Drop raw CSV files into `Nextcloud/Bookkeeping-System/bank-transactions/raw/`
2. Run the processor:

```bash
conda activate bookkeeping-system-env
cd /home/particle/Bookkeeping-System
python raw_processor.py
```

This will:
- Detect bank and account type from the filename
- Split transactions by month
- Write organized CSVs to the structured folder
- Append new rows to `master_transactions.csv` (with dedup)

**Supported filenames:** `cibc-business-cc.csv`, `cibc-business-dc.csv`, `cibc-personal-cc.csv`, `cibc-personal-dc.csv`, `cibc-personal-loc.csv`, `rbc-business-cc.csv`, `rbc-business-dc.csv`

Disable this script once n8n is live and calling `/ingest/transaction` directly.

---

## Dashboard

The dashboard is a password-protected web app served at `http://<device-ip>:5000`. All views support a `?month=YYYY-MM` filter via a dropdown in the top bar.

**Overview** — Combined KPIs (total in, total out, net), business and personal snapshots side by side, monthly trend bar chart (Chart.js), and a natural language query input powered by Claude Sonnet.

**Business** — Revenue sources and expense breakdown by category as horizontal bar charts, top vendors by spend, full transaction table with categorization source (rule / AI / manual) and flagged indicators.

**Personal** — Same layout as Business but for personal accounts — income sources, expense categories, vendor breakdown, transaction table.

**Flagged** — All transactions Claude categorized with confidence below 0.7, or that were flagged for other reasons. Shows confidence score colour-coded (red / amber / green) and flag reason. Includes an NL input to describe corrections.

**Rules** — Three sections:
1. *Suggested rules* — patterns Claude identified during the last batch run (vendors it categorized the same way 2+ times). One-click approve (writes to `rules.json`) or dismiss.
2. *NL rule editor* — describe a rule in plain English; Claude generates the JSON and proposes it for confirmation.
3. *Active rules table* — full read-only view of all rules in priority order, showing match conditions, applied fields, and P&L inclusion status.

**Design:** DM Sans + DM Mono fonts, cream background (`#f7f6f3`), teal (`#1d9e75`) for business, purple (`#7f77dd`) for personal. Fully mobile-responsive — sidebar collapses to a slide-in drawer with a hamburger menu on screens ≤ 768px.

---

## Routes

| Route | Method | Auth | Description |
|---|---|---|---|
| `/health` | GET | No | Health check — returns `{"status": "ok"}` |
| `/login` | GET/POST | No | Dashboard login |
| `/logout` | GET | No | Clear session |
| `/` or `/dashboard` | GET | Yes | Overview — combined P&L, trend chart, NL query |
| `/business` | GET | Yes | Business revenue, expenses by category, transaction table |
| `/personal` | GET | Yes | Personal income, expenses by category, transaction table |
| `/flagged` | GET | Yes | All flagged transactions needing review |
| `/rules` | GET | Yes | Active rules table + suggested rules + NL rule editor |
| `/rules/approve` | POST | Yes | Approve a suggested rule (adds to rules.json) |
| `/rules/dismiss` | POST | Yes | Dismiss a suggested rule |
| `/query` | POST | Yes | Natural language query (Claude Sonnet) |
| `/ingest/receipt` | POST | No | n8n webhook — receipt |
| `/ingest/transaction` | POST | No | n8n webhook — bank CSV |
| `/rules/propose` | POST | Yes | Propose a new rule via Telegram |
| `/rules/confirm` | POST | No | Telegram webhook — confirm rule |

All dashboard routes accept an optional `?month=YYYY-MM` query parameter to filter by month.

---

## Build phases

- [x] Phase 1 — Project scaffold, config, login, /health
- [x] Phase 2 — CSV utilities (safe read/append, dedup, create-if-missing)
- [x] Phase 3 — Rules engine (load rules.json, match, archive before write)
- [x] Phase 4 — Claude Haiku categorizer
- [x] Phase 5 — /ingest/transaction endpoint
- [x] Phase 6 — /ingest/receipt endpoint
- [x] Phase 7 — raw_processor.py (cron fallback for bank-transactions/raw/)
- [x] Phase 8 — Dashboard aggregator (P&L totals from CSV by month/category/account)
- [x] Phase 9 — Dashboard UI (overview, business, personal, flagged, rules — mobile-responsive)
- [ ] Phase 10 — NL query (/query endpoint, Claude Sonnet)
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
