# aggregator.py — reads master_transactions.csv and computes all dashboard numbers.
# Called on every request (no caching). Returns plain dicts — no Flask dependencies.

import csv
import logging
from collections import defaultdict
from pathlib import Path

from config import MASTER_TRANSACTIONS_CSV

log = logging.getLogger(__name__)


def _read_rows(month_filter=None, account_type_filter=None):
    """Return rows from master_transactions.csv, optionally filtered."""
    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except Exception as e:
        log.error("Failed to read master CSV: %s", e)
        return []

    if month_filter:
        rows = [r for r in rows if r["date"].startswith(month_filter)]
    if account_type_filter:
        rows = [r for r in rows if r["account_type"] == account_type_filter]
    return rows


def _available_months(rows=None):
    """Return sorted list of YYYY-MM strings present in the data."""
    if rows is None:
        rows = _read_rows()
    months = sorted(set(r["date"][:7] for r in rows if r.get("date")))
    return months


def _pnl_rows(rows):
    """Filter out rows excluded from P&L."""
    return [r for r in rows if r.get("exclude_from_pnl", "").strip().lower() != "true"]


def _amount(row):
    try:
        return float(row["amount"])
    except (ValueError, KeyError):
        return 0.0


# ---------------------------------------------------------------------------
# Overview aggregation
# ---------------------------------------------------------------------------

def get_overview(month_filter=None):
    """Return dict with top-level KPIs for the overview page."""
    all_rows = _read_rows(month_filter=month_filter)
    months = _available_months(_read_rows())  # always full list for dropdown

    biz_rows = _pnl_rows([r for r in all_rows if r["account_type"] == "business"])
    per_rows = _pnl_rows([r for r in all_rows if r["account_type"] == "personal"])

    biz_revenue  = sum(_amount(r) for r in biz_rows if _amount(r) > 0)
    biz_expenses = sum(_amount(r) for r in biz_rows if _amount(r) < 0)
    per_income   = sum(_amount(r) for r in per_rows if _amount(r) > 0)
    per_expenses = sum(_amount(r) for r in per_rows if _amount(r) < 0)

    total_in  = biz_revenue + per_income
    total_out = biz_expenses + per_expenses
    net       = total_in + total_out  # out is already negative

    # Month-by-month trend (for chart) — across all months, split by account_type
    trend = _build_trend(_read_rows(account_type_filter=None))

    flagged = [r for r in all_rows if r.get("flagged", "").strip().lower() == "true"]

    return {
        "months": months,
        "selected_month": month_filter or "all",
        "biz_revenue":  round(biz_revenue,  2),
        "biz_expenses": round(abs(biz_expenses), 2),
        "biz_net":      round(biz_revenue + biz_expenses, 2),
        "per_income":   round(per_income,   2),
        "per_expenses": round(abs(per_expenses), 2),
        "per_net":      round(per_income + per_expenses, 2),
        "total_in":     round(total_in,  2),
        "total_out":    round(abs(total_out), 2),
        "net":          round(net, 2),
        "trend":        trend,
        "flagged":      flagged,
        "flagged_count": len(flagged),
        "total_count":  len(all_rows),
    }


def _build_trend(rows):
    """Return list of {month, biz_net, per_net} dicts for the chart."""
    months = sorted(set(r["date"][:7] for r in rows if r.get("date")))
    result = []
    for m in months:
        m_rows = [r for r in rows if r["date"].startswith(m)]
        biz = _pnl_rows([r for r in m_rows if r["account_type"] == "business"])
        per = _pnl_rows([r for r in m_rows if r["account_type"] == "personal"])
        biz_net = sum(_amount(r) for r in biz)
        per_net = sum(_amount(r) for r in per)
        result.append({
            "month": m,
            "biz_net": round(biz_net, 2),
            "per_net": round(per_net, 2),
        })
    return result


# ---------------------------------------------------------------------------
# Business aggregation
# ---------------------------------------------------------------------------

def get_business(month_filter=None):
    """Return dict with business P&L breakdown by category."""
    rows = _read_rows(month_filter=month_filter, account_type_filter="business")
    months = _available_months(_read_rows())
    pnl = _pnl_rows(rows)

    revenue  = sum(_amount(r) for r in pnl if _amount(r) > 0)
    expenses = sum(_amount(r) for r in pnl if _amount(r) < 0)

    # Revenue by vendor
    rev_by_vendor = defaultdict(float)
    for r in pnl:
        if _amount(r) > 0:
            rev_by_vendor[r.get("vendor_name") or r.get("description", "Unknown")] += _amount(r)

    # Expenses by category
    exp_by_cat = defaultdict(float)
    for r in pnl:
        if _amount(r) < 0:
            exp_by_cat[r.get("category") or "Uncategorized"] += abs(_amount(r))

    # Expenses by vendor (top 10)
    exp_by_vendor = defaultdict(float)
    for r in pnl:
        if _amount(r) < 0:
            exp_by_vendor[r.get("vendor_name") or r.get("description", "Unknown")] += abs(_amount(r))

    # All transactions for the table
    transactions = sorted(rows, key=lambda r: r["date"], reverse=True)

    return {
        "months": months,
        "selected_month": month_filter or "all",
        "revenue":  round(revenue, 2),
        "expenses": round(abs(expenses), 2),
        "net":      round(revenue + expenses, 2),
        "revenue_by_vendor": _sort_dict(rev_by_vendor),
        "expenses_by_category": _sort_dict(exp_by_cat),
        "expenses_by_vendor": _sort_dict(exp_by_vendor, top=10),
        "transactions": transactions,
        "categories_tree": _build_categories_tree(_pnl_rows(rows)),
    }


# ---------------------------------------------------------------------------
# Personal aggregation
# ---------------------------------------------------------------------------

def get_personal(month_filter=None):
    """Return dict with personal income/expense breakdown by category."""
    rows = _read_rows(month_filter=month_filter, account_type_filter="personal")
    months = _available_months(_read_rows())
    pnl = _pnl_rows(rows)

    income   = sum(_amount(r) for r in pnl if _amount(r) > 0)
    expenses = sum(_amount(r) for r in pnl if _amount(r) < 0)

    inc_by_source = defaultdict(float)
    for r in pnl:
        if _amount(r) > 0:
            inc_by_source[r.get("vendor_name") or r.get("description", "Unknown")] += _amount(r)

    exp_by_cat = defaultdict(float)
    for r in pnl:
        if _amount(r) < 0:
            exp_by_cat[r.get("category") or "Uncategorized"] += abs(_amount(r))

    exp_by_vendor = defaultdict(float)
    for r in pnl:
        if _amount(r) < 0:
            exp_by_vendor[r.get("vendor_name") or r.get("description", "Unknown")] += abs(_amount(r))

    transactions = sorted(rows, key=lambda r: r["date"], reverse=True)

    return {
        "months": months,
        "selected_month": month_filter or "all",
        "income":   round(income, 2),
        "expenses": round(abs(expenses), 2),
        "net":      round(income + expenses, 2),
        "income_by_source": _sort_dict(inc_by_source),
        "expenses_by_category": _sort_dict(exp_by_cat),
        "expenses_by_vendor": _sort_dict(exp_by_vendor, top=10),
        "transactions": transactions,
        "categories_tree": _build_categories_tree(_pnl_rows(rows)),
    }


# ---------------------------------------------------------------------------
# Flagged transactions
# ---------------------------------------------------------------------------

def get_flagged(month_filter=None):
    """Return all flagged transactions."""
    rows = _read_rows(month_filter=month_filter)
    months = _available_months(_read_rows())
    flagged = [r for r in rows if r.get("flagged", "").strip().lower() == "true"]
    return {
        "months": months,
        "selected_month": month_filter or "all",
        "flagged": sorted(flagged, key=lambda r: r["date"], reverse=True),
        "count": len(flagged),
    }


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _sort_dict(d, top=None):
    """Sort dict by value descending, return list of (key, value) tuples."""
    items = sorted(d.items(), key=lambda x: x[1], reverse=True)
    if top:
        items = items[:top]
    return [(k, round(v, 2)) for k, v in items]


def _build_categories_tree(rows):
    """Build [{name, total, subcategories, transactions}] sorted by abs total.

    Each subcategory includes its individual transactions.
    Transactions that have no subcategory are attached directly to the category.
    Sign is preserved so the template can colour income green, expenses red.
    """
    cat_totals     = defaultdict(float)
    subcat_totals  = defaultdict(lambda: defaultdict(float))
    cat_direct     = defaultdict(list)   # transactions without a subcategory
    subcat_txns    = defaultdict(lambda: defaultdict(list))

    for r in rows:
        cat    = r.get("category") or "Uncategorized"
        subcat = (r.get("subcategory") or "").strip()
        amt    = _amount(r)
        cat_totals[cat] += amt
        txn = {
            "id":      r.get("transaction_id", ""),
            "date":    r.get("date", ""),
            "vendor":  r.get("vendor_name") or r.get("description", ""),
            "desc":    r.get("description", ""),
            "amount":  round(amt, 2),
            "account": r.get("account_type", ""),
            "cat":     r.get("category", ""),
            "subcat":  r.get("subcategory", ""),
        }
        if subcat:
            subcat_totals[cat][subcat] += amt
            subcat_txns[cat][subcat].append(txn)
        else:
            cat_direct[cat].append(txn)

    result = []
    for cat, total in sorted(cat_totals.items(), key=lambda x: abs(x[1]), reverse=True):
        subcats = []
        for sub, sub_total in sorted(subcat_totals[cat].items(), key=lambda x: abs(x[1]), reverse=True):
            txns = sorted(subcat_txns[cat][sub], key=lambda x: x["date"], reverse=True)
            subcats.append({"name": sub, "total": round(sub_total, 2), "transactions": txns})
        direct = sorted(cat_direct[cat], key=lambda x: x["date"], reverse=True)
        result.append({
            "name":           cat,
            "total":          round(total, 2),
            "subcategories":  subcats,
            "transactions":   direct,
        })
    return result
