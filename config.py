import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# =============================================================================
# PATHS
# All filesystem locations. Change NEXTCLOUD_BASE in .env — everything derives
# from it automatically.
# Used by: categorizer.py, dashboard/aggregator.py, dashboard/routes.py,
#          ingest/receipts.py, ingest/transactions.py, raw_processor.py
# =============================================================================
NEXTCLOUD_BASE          = Path(os.environ["NEXTCLOUD_BASE"])
MASTER_TRANSACTIONS_CSV = NEXTCLOUD_BASE / "master" / "master_transactions.csv"
MASTER_RECEIPTS_CSV     = NEXTCLOUD_BASE / "master" / "master_receipts.csv"
RULES_JSON              = NEXTCLOUD_BASE / "master" / "rules" / "rules.json"
RULES_ARCHIVE_DIR       = NEXTCLOUD_BASE / "master" / "rules"
SUGGESTED_RULES_FILE    = RULES_ARCHIVE_DIR / "rules_suggested.json"  # used by: categorizer.py


# =============================================================================
# CLAUDE API — Models and keys
# HAIKU_MODEL  : used for transaction categorization (categorizer.py)
# SONNET_MODEL : available as upgrade option in NL chat (query/nl.py)
# =============================================================================
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
HAIKU_MODEL       = "claude-haiku-4-5-20251001"   # categorizer.py, raw_processor.py
SONNET_MODEL      = "claude-sonnet-4-6"            # query/nl.py (optional upgrade)


# =============================================================================
# CATEGORIZATION THRESHOLDS
# CONFIDENCE_THRESHOLD : AI confidence below this → row is flagged for review
#                        used by: categorizer.py
# RULE_SUGGESTION_MIN  : vendor must appear this many times in a batch before
#                        it's surfaced as a suggested rule
#                        used by: categorizer.py → suggest_rules()
# =============================================================================
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", 0.7))
RULE_SUGGESTION_MIN  = 2   # categorizer.py → suggest_rules()

# Pass-through detection thresholds
# PASSTHROUGH_TOLERANCE  : max dollar difference between IN and OUT amounts to
#                          be considered a pass-through pair (default $1.00)
# PASSTHROUGH_WINDOW_DAYS: max calendar days between the IN and OUT transactions
#                          (default 5 days)
# Used by: dashboard/aggregator.py → detect_passthrough_pairs()
#          dashboard/routes.py → /passthrough/scan
PASSTHROUGH_TOLERANCE   = float(os.environ.get("PASSTHROUGH_TOLERANCE",   1.00))
PASSTHROUGH_WINDOW_DAYS = int(os.environ.get("PASSTHROUGH_WINDOW_DAYS",   2))


# =============================================================================
# MODEL PRICING — USD per 1 million tokens
# Used by: query/nl.py to calculate per-message and session cost
# Update these if Anthropic changes their pricing (console.anthropic.com/settings/billing)
# =============================================================================
NL_MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 0.80,  "output": 4.00},
    "claude-sonnet-4-6":         {"input": 3.00,  "output": 15.00},
}
USD_TO_CAD = 1.38   # rough exchange rate — update as needed


# =============================================================================
# NL QUERY — Models available in the chat UI
# NL_MODELS        : ordered cheapest first — drives the model selector dropdown
# NL_DEFAULT_MODEL : pre-selected model on page load
# NL_MAX_TOKENS    : max tokens Claude may return for a chat answer
# NL_HISTORY_LIMIT : how many prior turns to send with each request (each turn
#                    = 1 user message + 1 AI reply, so 20 = 10 exchanges)
# NL_TOP_VENDORS   : how many top vendors by spend to include in the summary
# NL_TOP_INCOME    : how many top income sources to include in the summary
# All used by: query/nl.py
# =============================================================================
NL_MODELS = [
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku",  "note": "Fast · cheapest"},
    {"id": "claude-sonnet-4-6",         "label": "Sonnet", "note": "Smarter · 5× cost"},
]
NL_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
NL_MAX_TOKENS    = 1024
NL_HISTORY_LIMIT = 20   # change here to adjust chat memory window
NL_TOP_VENDORS   = 15
NL_TOP_INCOME    = 10


# =============================================================================
# CATEGORIZER API CALL
# CATEGORIZER_MAX_TOKENS : max tokens for Claude's JSON categorization response
#                          keep this low — response is just a small JSON object
# Used by: categorizer.py → _claude_categorize()
# =============================================================================
CATEGORIZER_MAX_TOKENS = 256


# =============================================================================
# COST TRACKING
# HAIKU_COST_PER_CALL : estimated CAD cost per Haiku API call, used in
#                       raw_processor.py end-of-run summary log line
# Used by: raw_processor.py
# =============================================================================
HAIKU_COST_PER_CALL = 0.001   # CAD — adjust if Anthropic pricing changes


# =============================================================================
# AUTH
# DASHBOARD_PASSWORD : set in .env — the login password for the dashboard
# FLASK_SECRET_KEY   : signs session cookies — set a long random string in .env
# FLASK_PORT         : port Flask listens on (default 5000)
# AUTO_LOGIN         : set AUTO_LOGIN=true in .env to bypass login entirely
#                      (useful for local dev or debugging)
# Used by: app.py
# =============================================================================
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
FLASK_SECRET_KEY   = os.environ.get("FLASK_SECRET_KEY", "change-me")
FLASK_PORT         = int(os.environ.get("FLASK_PORT", 5000))
AUTO_LOGIN         = os.environ.get("AUTO_LOGIN", "false").strip().lower() == "true"


# =============================================================================
# TELEGRAM
# Used by: ingest/transactions.py (Phase 11 — rule proposal flow, not yet built)
# =============================================================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")


# =============================================================================
# VALID CATEGORIES
# Claude must return exactly one of these strings. Adding a new category here
# automatically makes it available to the categorizer and the dashboard.
# Used by: categorizer.py → _claude_categorize()
#          dashboard/aggregator.py (implicit — reads whatever is in the CSV)
# =============================================================================
BUSINESS_CATEGORIES = [
    "Revenue",
    "SaaS tools",
    "Contractors",
    "Insurance",
    "Hosting",
    "Advertising",
    "Banking fees",
    "Office supplies",
    "Professional services",
    "Credit card payment",
    "Uncategorized",
]

PERSONAL_CATEGORIES = [
    "Income",
    "Remittances",
    "Loan repayment",
    "Living expenses",
    "Insurance",
    "Subscriptions",
    "Healthcare",
    "Transport",
    "Credit card payment",
    "Uncategorized",
]

ALL_CATEGORIES = {
    "business": BUSINESS_CATEGORIES,
    "personal": PERSONAL_CATEGORIES,
}
