#!/usr/bin/env python3
"""
recategorize.py — Re-apply rules + Claude to all non-manual rows in master_transactions.csv.

Standalone:  python recategorize.py
Via Flask:   imported by dashboard/routes.py and called in a background thread.

Logic per row:
  - categorized_by == "manual"  → skip (preserve human overrides)
  - rule matches                → update to rule result (no API call)
  - no rule match               → call Claude Haiku, track tokens
Writes the full updated CSV back atomically (temp file → rename).
"""

import csv
import json
import shutil
import time
from pathlib import Path

import anthropic

from config import (
    MASTER_TRANSACTIONS_CSV, ANTHROPIC_API_KEY, HAIKU_MODEL,
    CATEGORIZER_MAX_TOKENS, CONFIDENCE_THRESHOLD,
    NL_MODEL_PRICING, USD_TO_CAD,
)
from settings_utils import get_categories, get_exclude_from_pnl_categories
from categorizer import load_rules, match_rule
from csv_utils import TRANSACTION_HEADERS
from logger import get_logger

log = get_logger("recategorize")

_PRICING = NL_MODEL_PRICING.get(HAIKU_MODEL, {"input": 0.80, "output": 4.00})


def _categorize_with_claude(row):
    """Call Claude Haiku for one row. Returns (result_dict, input_tokens, output_tokens)."""
    account_type = row.get("account_type", "business")
    _system_only = get_exclude_from_pnl_categories()
    valid_categories = [c for c in get_categories(account_type) if c not in _system_only]
    categories_str = "\n".join(f"  - {c}" for c in valid_categories)

    prompt = f"""Categorize this Canadian bank transaction. Return ONLY a JSON object — no explanation, no markdown.

Transaction details:
  Description : {row['description']}
  Account type: {account_type}
  Amount (CAD) : {row['amount']}  (negative = expense, positive = income)
  Bank        : {row.get('bank_name', '')}

Valid categories for a {account_type} account:
{categories_str}

Return exactly this JSON format:
{{
  "vendor_name": "clean business name extracted from the description",
  "category":    "one category from the list above — exact spelling",
  "subcategory": "your own descriptive label",
  "confidence":  0.85
}}"""

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    message = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=CATEGORIZER_MAX_TOKENS,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]

    result = json.loads(raw)
    confidence = float(result.get("confidence", 0.0))
    flagged = confidence < CONFIDENCE_THRESHOLD

    return (
        {
            "category":         result.get("category", "Uncategorized"),
            "subcategory":      result.get("subcategory", ""),
            "vendor_name":      result.get("vendor_name", row["description"]),
            "categorized_by":   "ai",
            "confidence":       confidence,
            "flagged":          flagged,
            "flag_reason":      "low confidence" if flagged else "",
        },
        message.usage.input_tokens,
        message.usage.output_tokens,
    )


def recategorize(progress=None):
    """
    Re-categorize all non-manual rows. Updates progress dict in-place for polling.
    Returns final stats dict.
    """
    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        raise FileNotFoundError(f"master_transactions.csv not found: {path}")

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    rules = load_rules()
    start = time.time()

    state = {
        "running":        True,
        "done":           False,
        "error":          None,
        "total":          len(rows),
        "processed":      0,
        "updated":        0,
        "skipped_manual": 0,
        "api_calls":      0,
        "input_tokens":   0,
        "output_tokens":  0,
        "cost_usd":       0.0,
        "cost_cad":       0.0,
        "elapsed":        0,
    }
    if progress is not None:
        progress.update(state)

    updated_rows = []

    for row in rows:
        elapsed = int(time.time() - start)

        if row.get("categorized_by") == "manual":
            state["skipped_manual"] += 1
            state["processed"] += 1
            state["elapsed"] = elapsed
            updated_rows.append(row)
            if progress is not None:
                progress.update(state)
            continue

        old_cat = row.get("category", "")
        apply, rule_id, _ = match_rule(row, rules)

        if apply:
            row["category"]        = apply.get("category", "Uncategorized")
            row["subcategory"]     = apply.get("subcategory", "")
            row["categorized_by"]  = "rule"
            row["confidence"]      = ""
            row["flagged"]         = str(apply.get("flagged", False))
            row["flag_reason"]     = apply.get("flag_reason", "")
            row["exclude_from_pnl"] = str(apply.get("exclude_from_pnl", False))
            if row["category"] != old_cat:
                state["updated"] += 1
        else:
            try:
                result, tok_in, tok_out = _categorize_with_claude(row)
                row["category"]       = result["category"]
                row["subcategory"]    = result["subcategory"]
                row["vendor_name"]    = result["vendor_name"]
                row["categorized_by"] = result["categorized_by"]
                row["confidence"]     = str(result["confidence"])
                row["flagged"]        = str(result["flagged"])
                row["flag_reason"]    = result["flag_reason"]
                state["api_calls"]   += 1
                state["input_tokens"]  += tok_in
                state["output_tokens"] += tok_out
                if row["category"] != old_cat:
                    state["updated"] += 1
            except Exception as e:
                log.error("Claude failed for '%s': %s", row.get("description", "?")[:50], e)

        state["processed"] += 1
        state["elapsed"]    = elapsed
        state["cost_usd"]   = (
            state["input_tokens"]  * _PRICING["input"] +
            state["output_tokens"] * _PRICING["output"]
        ) / 1_000_000
        state["cost_cad"] = state["cost_usd"] * USD_TO_CAD
        updated_rows.append(row)

        if progress is not None:
            progress.update(state)

    # Write back atomically
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRANSACTION_HEADERS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(updated_rows)
    shutil.move(str(tmp), str(path))

    state["running"] = False
    state["done"]    = True
    state["elapsed"] = int(time.time() - start)
    state["cost_usd"] = round(state["cost_usd"], 6)
    state["cost_cad"] = round(state["cost_cad"], 6)

    if progress is not None:
        progress.update(state)

    log.info(
        "Recategorize done: %d rows, %d updated, %d skipped (manual), "
        "%d API calls, $%.4f CAD, %ds",
        state["total"], state["updated"], state["skipped_manual"],
        state["api_calls"], state["cost_cad"], state["elapsed"],
    )
    return state


if __name__ == "__main__":
    print("Recategorizing master_transactions.csv ...")
    stats = recategorize()
    print(f"\n  Done in {stats['elapsed']}s")
    print(f"  Rows total:      {stats['total']}")
    print(f"  Updated:         {stats['updated']}")
    print(f"  Skipped (manual):{stats['skipped_manual']}")
    print(f"  API calls:       {stats['api_calls']}")
    print(f"  Tokens in/out:   {stats['input_tokens']} / {stats['output_tokens']}")
    print(f"  Cost:            ${stats['cost_cad']:.4f} CAD (${stats['cost_usd']:.4f} USD)")
