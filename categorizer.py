"""
categorizer.py — Rules engine and Claude Haiku categorization.

HOW CATEGORIZATION WORKS (two-step pipeline):

  Step 1 — Rules engine (this file, Phase 3)
    Loads rules.json and checks each rule in order against the transaction.
    Matching is case-insensitive on the vendor description.
    First match wins — no further rules are checked.
    Rules are deterministic and free (no API call).

  Step 2 — Claude Haiku fallback (Phase 4, stubbed below)
    If no rule matches, the transaction is sent to Claude Haiku.
    Claude picks a category + subcategory and returns a confidence score.
    If confidence < CONFIDENCE_THRESHOLD (default 0.7), the row is flagged
    for manual review.

  The main entry point is categorize(transaction). Everything else in this
  file is a helper that categorize() calls internally.

WHAT categorize() RETURNS:
  A dict with these fields, ready to be merged into a master CSV row:
  {
    "category":         str,
    "subcategory":      str,
    "vendor_name":      str,   # cleaned name (rules use raw desc for now)
    "categorized_by":   "rule" | "ai" | "manual",
    "confidence":       float | None,   # None for rule/manual
    "flagged":          bool,
    "flag_reason":      str,
    "exclude_from_pnl": bool,
    "notes":            str,
  }

RULE SUGGESTION (future — Phase 10/11):
  After a batch of AI categorizations, the system will surface repeated
  patterns as suggested rules. You approve them in the dashboard and they
  get written to rules.json via archive_rules() + write.
"""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

from config import (
    RULES_JSON, RULES_ARCHIVE_DIR,
    ANTHROPIC_API_KEY, HAIKU_MODEL,
    CONFIDENCE_THRESHOLD, ALL_CATEGORIES
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rules loading
# ---------------------------------------------------------------------------

def load_rules():
    """Read rules.json and return the list of rules.

    Called at the start of every ingest batch — not cached — so any edits
    to rules.json (via UI or direct file edit) are picked up immediately
    without restarting the app.

    Returns:
        List of rule dicts from the "rules" key in rules.json.
        Empty list if the file is missing or malformed.
    """
    if not RULES_JSON.exists():
        log.warning("rules.json not found — no rules will be applied")
        return []
    try:
        with open(RULES_JSON, encoding="utf-8") as f:
            data = json.load(f)
        rules = data.get("rules", [])
        log.info(f"Loaded {len(rules)} rules from rules.json")
        return rules
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Failed to load rules.json: {e}")
        return []


# ---------------------------------------------------------------------------
# Rules matching
# ---------------------------------------------------------------------------

def match_rule(transaction, rules):
    """Check a transaction against all rules and return the first match.

    Matching logic:
      - vendor_name_contains: case-insensitive substring match against the
        transaction description (raw bank text). This is intentionally loose
        so "GOHIGHLEVEL" matches "HIGHLEVEL AGENCY SUB GOHIGHLEVEL.C, TX".
      - account_type: exact match (personal | business).
      - Both conditions must be true for a rule to match.
      - Rules are checked in order — first match wins, rest are ignored.

    Args:
        transaction: dict with at least "description" and "account_type" keys
        rules:       list of rule dicts loaded by load_rules()

    Returns:
        The matching rule's "apply" dict if a rule matched, or None.
        Also returns the matched rule's id and description for logging.
    """
    description = transaction.get("description", "").lower()
    account_type = transaction.get("account_type", "")

    for rule in rules:
        match = rule.get("match", {})
        keyword = match.get("vendor_name_contains", "").lower()
        rule_account = match.get("account_type", "")

        # Both conditions must match
        keyword_matches = keyword and keyword in description
        account_matches = not rule_account or rule_account == account_type

        if keyword_matches and account_matches:
            log.debug(f"Rule {rule['id']} matched: {transaction['description'][:50]}")
            return rule.get("apply", {}), rule["id"], rule["description"]

    return None, None, None


# ---------------------------------------------------------------------------
# Rules archiving (call before every write to rules.json)
# ---------------------------------------------------------------------------

def archive_rules():
    """Copy the current rules.json to a timestamped backup before overwriting.

    This is called automatically before any write to rules.json — whether
    from the dashboard UI, a natural language command, or the rule proposal
    flow. Direct file edits bypass this (by design, as an escape hatch).

    Archive filename format: rules_YYYY-MM-DD_HHMMSS.json
    Archive location: same folder as rules.json

    Returns:
        Path to the archive file, or None if archiving failed.
    """
    if not RULES_JSON.exists():
        log.warning("No rules.json to archive — skipping")
        return None
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    archive_path = RULES_ARCHIVE_DIR / f"rules_{timestamp}.json"
    try:
        shutil.copy2(RULES_JSON, archive_path)
        log.info(f"Archived rules.json → {archive_path.name}")
        return archive_path
    except OSError as e:
        log.error(f"Failed to archive rules.json: {e}")
        return None


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def categorize(transaction, rules):
    """Categorize a single transaction. Rules first, Claude fallback second.

    This is the only function ingest endpoints and raw_processor should call.
    Pass the full rules list (loaded once per batch) to avoid re-reading
    rules.json on every transaction.

    Args:
        transaction: dict with keys: description, account_type, amount, bank_name
        rules:       list from load_rules() — load once per batch, pass here

    Returns:
        dict with categorization fields ready to merge into a CSV row.
        See module docstring for the full field list.
    """
    # --- Step 1: try rules ---
    apply, rule_id, rule_desc = match_rule(transaction, rules)

    if apply:
        log.info(f"Categorized by {rule_id}: {transaction['description'][:50]}")
        return {
            "category":         apply.get("category", "Uncategorized"),
            "subcategory":      apply.get("subcategory", ""),
            "vendor_name":      transaction["description"],  # will be cleaned in Phase 4
            "categorized_by":   "rule",
            "confidence":       None,   # rules are deterministic — no confidence score
            "flagged":          apply.get("flagged", False),
            "flag_reason":      apply.get("flag_reason", ""),
            "exclude_from_pnl": apply.get("exclude_from_pnl", False),
            "notes":            apply.get("notes", ""),
        }

    # --- Step 2: Claude Haiku fallback (Phase 4) ---
    return _claude_categorize(transaction)


def _claude_categorize(transaction):
    """Call Claude Haiku to categorize a transaction that matched no rule.

    Phase 4 stub — returns Uncategorized and flags for review until
    the Claude API integration is built.
    """
    # TODO (Phase 4): call Claude Haiku API here
    log.info(f"No rule matched — flagged for review: {transaction['description'][:50]}")
    return {
        "category":         "Uncategorized",
        "subcategory":      "",
        "vendor_name":      transaction["description"],
        "categorized_by":   "",
        "confidence":       None,
        "flagged":          True,
        "flag_reason":      "no rule matched — pending AI categorization",
        "exclude_from_pnl": False,
        "notes":            "",
    }
