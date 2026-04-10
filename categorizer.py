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
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import anthropic

from config import (
    RULES_JSON, RULES_ARCHIVE_DIR,
    ANTHROPIC_API_KEY, HAIKU_MODEL,
    CONFIDENCE_THRESHOLD, ALL_CATEGORIES
)

SUGGESTED_RULES_FILE = RULES_ARCHIVE_DIR.parent / "rules_suggested.json"

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
            "vendor_name":      transaction["description"],
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

    Sends the raw description, account type, amount, and the valid category
    list to Haiku. Expects a JSON response with vendor_name, category,
    subcategory, and confidence.

    On any API or parse failure: logs the error, flags the row, and returns
    Uncategorized — never crashes the ingest process.
    """
    account_type = transaction.get("account_type", "business")
    valid_categories = ALL_CATEGORIES.get(account_type, ALL_CATEGORIES["business"])
    categories_str = "\n".join(f"  - {c}" for c in valid_categories)

    prompt = f"""Categorize this Canadian bank transaction. Return ONLY a JSON object — no explanation, no markdown.

Transaction details:
  Description : {transaction['description']}
  Account type: {account_type}
  Amount (CAD) : {transaction['amount']}  (negative = expense, positive = income)
  Bank        : {transaction.get('bank_name', '')}

Valid categories for a {account_type} account:
{categories_str}

Return exactly this JSON format:
{{
  "vendor_name": "clean business name extracted from the description",
  "category":    "one category from the list above — exact spelling",
  "subcategory": "your own descriptive label (e.g. Gas station, Restaurant, Cloud storage)",
  "confidence":  0.85
}}

Guidelines:
- vendor_name should be the recognisable business name, not the raw bank string
- category must match one of the valid categories exactly
- confidence is how certain you are: 1.0 = certain, 0.5 = guessing
- If you genuinely cannot determine the category, use "Uncategorized" with low confidence"""

    try:
        client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        message = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = message.content[0].text.strip()

        # Strip markdown fences if Claude wraps the JSON anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)
        confidence = float(result.get("confidence", 0.0))
        flagged = confidence < CONFIDENCE_THRESHOLD

        log.info(
            f"AI categorized '{transaction['description'][:40]}' → "
            f"{result.get('category')} ({confidence:.2f})"
            + (" [FLAGGED]" if flagged else "")
        )
        return {
            "category":         result.get("category", "Uncategorized"),
            "subcategory":      result.get("subcategory", ""),
            "vendor_name":      result.get("vendor_name", transaction["description"]),
            "categorized_by":   "ai",
            "confidence":       confidence,
            "flagged":          flagged,
            "flag_reason":      "low confidence" if flagged else "",
            "exclude_from_pnl": False,
            "notes":            "",
        }

    except json.JSONDecodeError as e:
        log.error(f"Haiku returned invalid JSON for '{transaction['description'][:50]}': {e}")
        return _fallback(transaction, "AI returned invalid JSON")
    except Exception as e:
        log.error(f"Haiku API error for '{transaction['description'][:50]}': {e}")
        return _fallback(transaction, f"API error")


def _fallback(transaction, reason):
    """Safe fallback result when Claude API fails — never crashes ingest."""
    return {
        "category":         "Uncategorized",
        "subcategory":      "",
        "vendor_name":      transaction["description"],
        "categorized_by":   "",
        "confidence":       None,
        "flagged":          True,
        "flag_reason":      reason,
        "exclude_from_pnl": False,
        "notes":            "",
    }


# ---------------------------------------------------------------------------
# Rule suggestion — surfaces patterns from AI categorizations
# ---------------------------------------------------------------------------

def suggest_rules(ai_categorized_rows):
    """Look for repeated patterns in AI-categorized transactions and suggest rules.

    After a batch run, any vendor that Claude categorized the same way 2 or
    more times is a strong candidate for a permanent rule — so next time it's
    handled instantly without an API call.

    Suggestions are written to rules_suggested.json in the rules folder.
    The dashboard (Phase 9) will display them for one-click approval.

    Args:
        ai_categorized_rows: list of dicts, each with keys:
            description, account_type, vendor_name, category, subcategory
    """
    # Count (vendor_name, account_type, category, subcategory) combinations
    counts = defaultdict(int)
    examples = {}
    for row in ai_categorized_rows:
        key = (
            row.get("vendor_name", ""),
            row.get("account_type", ""),
            row.get("category", ""),
            row.get("subcategory", ""),
        )
        counts[key] += 1
        examples[key] = row.get("description", "")

    # Threshold: seen 2+ times → worth suggesting as a rule
    suggestions = []
    for (vendor, account_type, category, subcategory), count in counts.items():
        if count >= 2 and vendor and category and category != "Uncategorized":
            suggestions.append({
                "vendor_name":  vendor,
                "account_type": account_type,
                "category":     category,
                "subcategory":  subcategory,
                "seen_count":   count,
                "example_desc": examples[(vendor, account_type, category, subcategory)],
            })

    if not suggestions:
        log.info("No rule suggestions generated from this batch")
        return

    # Write to rules_suggested.json (overwrites — always reflects latest batch)
    try:
        with open(SUGGESTED_RULES_FILE, "w") as f:
            json.dump({"generated": datetime.now().isoformat(), "suggestions": suggestions}, f, indent=2)
        log.info(f"Wrote {len(suggestions)} rule suggestions → rules_suggested.json")
        print(f"\n  {len(suggestions)} rule suggestions saved — review them in the dashboard")
    except OSError as e:
        log.error(f"Failed to write rule suggestions: {e}")
