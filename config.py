import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Paths
NEXTCLOUD_BASE = Path(os.environ["NEXTCLOUD_BASE"])
MASTER_TRANSACTIONS_CSV = NEXTCLOUD_BASE / "master" / "master_transactions.csv"
MASTER_RECEIPTS_CSV     = NEXTCLOUD_BASE / "master" / "master_receipts.csv"
RULES_JSON              = NEXTCLOUD_BASE / "master" / "rules" / "rules.json"
RULES_ARCHIVE_DIR       = NEXTCLOUD_BASE / "master" / "rules"

# API
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY")
HAIKU_MODEL          = "claude-haiku-4-5-20251001"
SONNET_MODEL         = "claude-sonnet-4-6"
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", 0.7))

# Models available for NL queries — ordered cheapest first
NL_MODELS = [
    {"id": "claude-haiku-4-5-20251001",  "label": "Haiku",  "note": "Fast · cheapest"},
    {"id": "claude-sonnet-4-6",           "label": "Sonnet", "note": "Smarter · 5× cost"},
]
NL_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Auth
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD")
FLASK_SECRET_KEY   = os.environ.get("FLASK_SECRET_KEY", "change-me")
FLASK_PORT         = int(os.environ.get("FLASK_PORT", 5000))

# Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID")

# Valid categories — Claude must only return one of these
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
    "Uncategorized",
]

ALL_CATEGORIES = {
    "business": BUSINESS_CATEGORIES,
    "personal": PERSONAL_CATEGORIES,
}
