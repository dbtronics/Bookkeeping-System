# nl.py — natural language query endpoint.
# Builds a compact summary of master_transactions.csv and sends it to Claude
# along with the user's question. Returns a plain-English answer.
# Model is caller-selectable; defaults to Haiku (cheapest).

import csv
import json
import logging
from collections import defaultdict
from pathlib import Path

import anthropic
from flask import Blueprint, request, jsonify, session, redirect, url_for
from functools import wraps

from config import (
    ANTHROPIC_API_KEY, MASTER_TRANSACTIONS_CSV,
    NL_MODELS, NL_DEFAULT_MODEL,
)
from logger import get_logger

query_bp = Blueprint("query", __name__)
log = get_logger("query.nl")

# Valid model IDs (whitelist — never pass arbitrary user strings to the API)
_VALID_MODEL_IDS = {m["id"] for m in NL_MODELS}


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

def _build_summary(scope="all"):
    """
    Read master_transactions.csv and produce a compact text summary
    Claude can reason over without seeing every raw row.
    scope: "all" | "business" | "personal"
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

    if not rows:
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

    top_vendors = sorted(by_vendor.items(), key=lambda x: x[1], reverse=True)[:15]
    vendor_lines = "\n".join(f"  {v}: ${a:,.2f}" for v, a in top_vendors)

    # Top income sources
    by_income = defaultdict(float)
    for r in pnl:
        if amt(r) > 0:
            name = r.get("vendor_name") or r.get("description", "Unknown")
            by_income[name] += amt(r)

    top_income = sorted(by_income.items(), key=lambda x: x[1], reverse=True)[:10]
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

    summary = f"""BOOKKEEPING SUMMARY ({scope.upper()})
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
"""
    return summary


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _ask_claude(question, summary, model_id):
    """Send question + data summary to Claude. Returns answer string."""
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    system = (
        "You are a bookkeeping assistant for a small household and business. "
        "You are given a summary of financial transactions and must answer the user's question "
        "in plain English. Be concise and specific. Use dollar amounts where relevant. "
        "If the answer is not in the data, say so clearly. Do not make up numbers."
    )

    user_msg = f"""Here is the financial data summary:

{summary}

Question: {question}"""

    response = client.messages.create(
        model=model_id,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
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

    log.info("NL query | model=%s scope=%s | %s", model_id, scope, question[:80])

    try:
        t0 = time.time()
        summary = _build_summary(effective_scope)
        answer = _ask_claude(question, summary, model_id)
        elapsed = time.time() - t0
        log.info("NL query answered in %.1fs | model=%s scope=%s", elapsed, model_id, scope)
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
    from config import NL_MODELS, NL_DEFAULT_MODEL
    return jsonify({"models": NL_MODELS, "default": NL_DEFAULT_MODEL})
