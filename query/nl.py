# nl.py — natural language query endpoint.
# Builds a compact summary of master_transactions.csv and sends it to Claude
# along with the user's question. Returns a plain-English answer.
# Model is caller-selectable; defaults to Haiku (cheapest).

import csv
import json
import logging
import re
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import anthropic
from flask import Blueprint, request, jsonify, session, redirect, url_for
from functools import wraps

from config import (
    ANTHROPIC_API_KEY, MASTER_TRANSACTIONS_CSV,
    NL_MODELS, NL_DEFAULT_MODEL, NL_MAX_TOKENS,
    NL_HISTORY_LIMIT, NL_TOP_VENDORS, NL_TOP_INCOME,
)
from logger import get_logger

query_bp = Blueprint("query", __name__)
log = get_logger("query.nl")

# Valid model IDs (whitelist — never pass arbitrary user strings to the API)
_VALID_MODEL_IDS = {m["id"] for m in NL_MODELS}

# Month name → zero-padded month number
_MONTH_NAMES = {
    "january": "01", "jan": "01",
    "february": "02", "feb": "02",
    "march": "03", "mar": "03",
    "april": "04", "apr": "04",
    "may": "05",
    "june": "06", "jun": "06",
    "july": "07", "jul": "07",
    "august": "08", "aug": "08",
    "september": "09", "sep": "09", "sept": "09",
    "october": "10", "oct": "10",
    "november": "11", "nov": "11",
    "december": "12", "dec": "12",
}


def _detect_month_filter(question, available_years):
    """Detect a month reference in the question and return (YYYY-MM, note).

    Handles:
      - Named months: "january", "jan", etc.
      - Explicit year: "january 2025" or "2025 january"
      - "this month" / "last month" relative to today
      - No mention → returns (None, None)

    When a month is found but no year is stated, picks the most recent year
    in available_years that contains that month, and returns a note so
    Claude can tell the user what year was assumed.
    """
    q = question.lower()
    today = datetime.now()

    # "this month" / "last month"
    if "this month" in q:
        return today.strftime("%Y-%m"), None
    if "last month" in q:
        if today.month == 1:
            return f"{today.year - 1}-12", None
        return f"{today.year}-{today.month - 1:02d}", None

    # Look for explicit YYYY-MM or MM-YYYY patterns
    m = re.search(r'\b(20\d{2})[-/]?(0[1-9]|1[0-2])\b', q)
    if m:
        return f"{m.group(1)}-{m.group(2)}", None

    # Look for "month YYYY" or "YYYY month"
    for name, num in _MONTH_NAMES.items():
        pattern = rf'\b{name}\s+(20\d{{2}})\b|\b(20\d{{2}})\s+{name}\b'
        m = re.search(pattern, q)
        if m:
            year = m.group(1) or m.group(2)
            return f"{year}-{num}", None

    # Month name only — no year stated
    for name, num in _MONTH_NAMES.items():
        if re.search(rf'\b{name}\b', q):
            # Pick the most recent year in the data that has this month
            candidates = [y for y in sorted(available_years, reverse=True)
                          if f"{y}-{num}" in available_years.get(y, set())]
            if candidates:
                year = candidates[0]
            elif available_years:
                year = max(available_years.keys())
            else:
                year = today.year
            note = f"No year specified — assuming {year} based on your data. Correct me if you meant a different year."
            return f"{year}-{num}", note

    return None, None


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return jsonify({"error": "Not authenticated"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Data summary builder
# ---------------------------------------------------------------------------

def _build_available_years(rows):
    """Build {year: set_of_YYYY-MM} from all rows — used by month detector."""
    available = defaultdict(set)
    for r in rows:
        date = r.get("date", "")
        if len(date) >= 7:
            year = int(date[:4])
            ym = date[:7]
            available[year].add(ym)
    return dict(available)


def _build_summary(scope="all", month_filter=None, month_note=None):
    """
    Read master_transactions.csv and produce a compact text summary
    Claude can reason over without seeing every raw row.
    scope: "all" | "business" | "personal"
    month_filter: "YYYY-MM" string — when set, restricts all data to that month
    month_note: string shown at top of summary when year was assumed
    """
    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return "No transaction data available."

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    # Filter by scope
    if scope == "business":
        rows = [r for r in rows if r["account_type"] == "business"]
    elif scope == "personal":
        rows = [r for r in rows if r["account_type"] == "personal"]

    # Filter by month — all sections below operate on this filtered set
    if month_filter:
        rows = [r for r in rows if r.get("date", "").startswith(month_filter)]

    if not rows:
        if month_filter:
            return f"No transactions found for {month_filter} (scope: {scope})."
        return f"No transactions found for scope: {scope}."

    def amt(r):
        try: return float(r["amount"])
        except: return 0.0

    # P&L rows (exclude inter-account transfers)
    pnl = [r for r in rows if r.get("exclude_from_pnl", "").lower() != "true"]

    # Date range
    dates = sorted(r["date"] for r in rows if r.get("date"))
    date_range = f"{dates[0]} to {dates[-1]}" if dates else "unknown"

    # Totals
    total_in  = sum(amt(r) for r in pnl if amt(r) > 0)
    total_out = sum(amt(r) for r in pnl if amt(r) < 0)

    # By month
    monthly = defaultdict(lambda: {"in": 0.0, "out": 0.0})
    for r in pnl:
        m = r["date"][:7]
        if amt(r) > 0: monthly[m]["in"]  += amt(r)
        else:           monthly[m]["out"] += amt(r)

    monthly_lines = "\n".join(
        f"  {m}: in=${v['in']:,.2f}  out=${abs(v['out']):,.2f}  net=${v['in']+v['out']:,.2f}"
        for m, v in sorted(monthly.items())
    )

    # By category
    by_cat = defaultdict(float)
    for r in pnl:
        by_cat[f"{r['account_type']} / {r['category'] or 'Uncategorized'}"] += amt(r)

    cat_lines = "\n".join(
        f"  {cat}: ${v:,.2f}"
        for cat, v in sorted(by_cat.items(), key=lambda x: x[1])
    )

    # Top vendors by spend (expenses only)
    by_vendor = defaultdict(float)
    for r in pnl:
        if amt(r) < 0:
            name = r.get("vendor_name") or r.get("description", "Unknown")
            by_vendor[name] += abs(amt(r))

    top_vendors = sorted(by_vendor.items(), key=lambda x: x[1], reverse=True)[:NL_TOP_VENDORS]
    vendor_lines = "\n".join(f"  {v}: ${a:,.2f}" for v, a in top_vendors)

    # Top income sources
    by_income = defaultdict(float)
    for r in pnl:
        if amt(r) > 0:
            name = r.get("vendor_name") or r.get("description", "Unknown")
            by_income[name] += amt(r)

    top_income = sorted(by_income.items(), key=lambda x: x[1], reverse=True)[:NL_TOP_INCOME]
    income_lines = "\n".join(f"  {v}: ${a:,.2f}" for v, a in top_income)

    # Flagged count
    flagged = sum(1 for r in rows if r.get("flagged", "").lower() == "true")

    # Transactions excluded from P&L — list every one so Claude can name them
    excluded = [r for r in rows if r.get("exclude_from_pnl", "").lower() == "true"]
    excluded_lines = "\n".join(
        f"  {r['date']}  {r['account_type']}/{r['card_type']}  "
        f"{r.get('vendor_name') or r.get('description', '?')}  "
        f"${amt(r):,.2f}  [{r.get('subcategory') or r.get('category') or 'no reason'}]"
        for r in sorted(excluded, key=lambda r: r["date"])
    ) or "  None"

    # Flagged transactions — list them too
    flagged_rows = [r for r in rows if r.get("flagged", "").lower() == "true"]
    flagged_lines = "\n".join(
        f"  {r['date']}  {r['account_type']}  "
        f"{r.get('vendor_name') or r.get('description', '?')}  "
        f"${amt(r):,.2f}  confidence={r.get('confidence') or 'n/a'}  [{r.get('flag_reason') or ''}]"
        for r in sorted(flagged_rows, key=lambda r: r["date"])
    ) or "  None"

    # All individual transactions — sorted by date so Claude can answer date-specific
    # questions ("what date did I receive X", "break down January by transaction")
    all_txn_lines = "\n".join(
        f"  {r['date']}  {r['account_type']}/{r.get('card_type','')}  "
        f"{r.get('vendor_name') or r.get('description', '?')}  "
        f"${amt(r):,.2f}  [{r.get('category') or 'Uncategorized'}]"
        f"{'  [excluded from P&L]' if r.get('exclude_from_pnl','').lower()=='true' else ''}"
        for r in sorted(rows, key=lambda r: r["date"])
    ) or "  None"

    filter_label = f" — filtered to {month_filter}" if month_filter else ""
    assumption_note = f"\nNOTE FOR CLAUDE: {month_note}" if month_note else ""

    summary = f"""BOOKKEEPING SUMMARY ({scope.upper()}{filter_label}){assumption_note}
Date range: {date_range}
Total transactions: {len(rows)} ({len(pnl)} included in P&L, {len(excluded)} excluded from P&L, {flagged} flagged)
Total in (P&L): ${total_in:,.2f}
Total out (P&L): ${abs(total_out):,.2f}
Net: ${total_in + total_out:,.2f}

MONTHLY BREAKDOWN:
{monthly_lines}

BY CATEGORY (negative = expense, positive = income):
{cat_lines}

TOP VENDORS BY SPEND:
{vendor_lines}

TOP INCOME SOURCES:
{income_lines}

TRANSACTIONS EXCLUDED FROM P&L (inter-account transfers, CC payments, owner draws):
{excluded_lines}

FLAGGED TRANSACTIONS (low confidence or need review):
{flagged_lines}

ALL TRANSACTIONS (date / account / vendor / amount / category):
{all_txn_lines}
"""
    return summary


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _ask_claude(question, summary, model_id, history=None):
    """Send question + data summary to Claude. Returns answer string.

    history: list of {role, content} dicts from prior turns in this session.
    The data summary is injected only into the first user message so it's not
    repeated on every follow-up (saves tokens, avoids confusion).
    """
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = (
        "You are a bookkeeping assistant for a small household and business. "
        "You are given a summary of financial transactions and must answer the user's question "
        "in plain English. Be concise and specific. Use dollar amounts where relevant. "
        "If the answer is not in the data, say so clearly. Do not make up numbers. "
        "The data summary is provided in the first message of this conversation — use it "
        "to answer all follow-up questions."
    )

    messages = []

    if history:
        # First message in history already contains the data summary (injected below).
        # Subsequent history turns are plain question/answer pairs.
        for i, turn in enumerate(history):
            if turn["role"] == "user" and i == 0:
                # Prepend the summary to the very first user message
                messages.append({
                    "role": "user",
                    "content": f"Here is the financial data summary:\n\n{summary}\n\nQuestion: {turn['content']}"
                })
            else:
                messages.append({"role": turn["role"], "content": turn["content"]})
        # Now append the current question
        messages.append({"role": "user", "content": question})
    else:
        # No history — first message, include the summary
        messages.append({
            "role": "user",
            "content": f"Here is the financial data summary:\n\n{summary}\n\nQuestion: {question}"
        })

    response = client.messages.create(
        model=model_id,
        max_tokens=NL_MAX_TOKENS,
        system=system,
        messages=messages,
    )

    return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------

@query_bp.route("/query", methods=["POST"])
@login_required
def nl_query():
    data = request.get_json(silent=True) or {}

    question = (data.get("question") or "").strip()
    if not question:
        return jsonify({"error": "No question provided"}), 400

    scope = data.get("scope", "all")
    if scope not in ("all", "business", "personal", "rules"):
        scope = "all"

    # Model selection — whitelist only
    model_id = data.get("model", NL_DEFAULT_MODEL)
    if model_id not in _VALID_MODEL_IDS:
        model_id = NL_DEFAULT_MODEL

    # For rules scope, treat as all-data query
    effective_scope = "all" if scope == "rules" else scope

    # Conversation history — list of {role, content} from prior turns
    history = data.get("history", [])
    if not isinstance(history, list):
        history = []
    # Sanitize: only allow known roles, string content, cap at 20 turns
    history = [
        {"role": h["role"], "content": str(h["content"])}
        for h in history
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
    ][-NL_HISTORY_LIMIT:]

    log.info("NL query | model=%s scope=%s history=%d | %s", model_id, scope, len(history), question[:80])

    try:
        t0 = time.time()

        # Detect month mention in question — load all rows first to know available years
        try:
            with open(MASTER_TRANSACTIONS_CSV, newline="", encoding="utf-8") as f:
                all_rows = list(csv.DictReader(f))
        except OSError:
            all_rows = []
        available_years = _build_available_years(all_rows)
        month_filter, month_note = _detect_month_filter(question, available_years)
        if month_filter:
            log.info("Month filter detected: %s%s", month_filter,
                     " (year assumed)" if month_note else "")

        summary = _build_summary(effective_scope, month_filter=month_filter, month_note=month_note)
        answer = _ask_claude(question, summary, model_id, history=history)
        elapsed = time.time() - t0
        log.info("NL query answered in %.1fs | model=%s scope=%s month=%s",
                 elapsed, model_id, scope, month_filter or "all")
        return jsonify({"answer": answer, "model": model_id, "scope": scope})
    except Exception as e:
        log.error("NL query failed | model=%s | %s | error: %s", model_id, question[:60], e)
        return jsonify({"error": f"Query failed: {str(e)}"}), 500


# ---------------------------------------------------------------------------
# Config endpoint — returns available models for the UI dropdown
# ---------------------------------------------------------------------------

@query_bp.route("/query/models", methods=["GET"])
@login_required
def query_models():
    return jsonify({"models": NL_MODELS, "default": NL_DEFAULT_MODEL})
