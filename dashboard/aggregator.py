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

    kws = _load_transfer_keywords()
    rev_by_vendor  = defaultdict(float)
    rev_txns       = defaultdict(list)
    exp_by_cat     = defaultdict(float)
    exp_cat_txns   = defaultdict(list)
    exp_by_vendor  = defaultdict(float)
    exp_vnd_txns   = defaultdict(list)

    for r in pnl:
        amt = _amount(r)
        if amt > 0:
            key = _vendor_display(r)
            rev_by_vendor[key] += amt
            rev_txns[key].append(_txn_min(r, kws))
        else:
            cat = r.get("category") or "Uncategorized"
            exp_by_cat[cat] += abs(amt)
            exp_cat_txns[cat].append(_txn_min(r, kws))
            vnd = _vendor_display(r)
            exp_by_vendor[vnd] += abs(amt)
            exp_vnd_txns[vnd].append(_txn_min(r, kws))

    # All transactions for the table
    transactions = sorted(rows, key=lambda r: r["date"], reverse=True)

    return {
        "months": months,
        "selected_month": month_filter or "all",
        "revenue":  round(revenue, 2),
        "expenses": round(abs(expenses), 2),
        "net":      round(revenue + expenses, 2),
        "revenue_by_vendor":    _bar_items(rev_by_vendor,  rev_txns),
        "expenses_by_category": _bar_items(exp_by_cat,     exp_cat_txns),
        "expenses_by_vendor":   _bar_items(exp_by_vendor,  exp_vnd_txns, top=10),
        "transactions": transactions,
        "categories_tree": _build_categories_tree(_pnl_rows(rows)),
    }


# ---------------------------------------------------------------------------
# Personal aggregation
# ---------------------------------------------------------------------------

def _vendor_display(row):
    """Return a clean display name for a transaction row.

    For internal transfers (identified by keyword or subcategory), returns
    'Internal Transfer' instead of the raw bank description string.
    """
    kws = _load_transfer_keywords()
    vendor = (row.get("vendor_name") or row.get("description", "Unknown")).strip()
    desc   = row.get("description", "").lower()
    if any(k.lower() in desc for k in kws) or any(k.lower() in vendor.lower() for k in kws):
        return "Internal Transfer"
    return vendor or "Unknown"


def get_personal(month_filter=None):
    """Return dict with personal income/expense breakdown by category."""
    rows = _read_rows(month_filter=month_filter, account_type_filter="personal")
    months = _available_months(_read_rows())
    pnl = _pnl_rows(rows)

    income   = sum(_amount(r) for r in pnl if _amount(r) > 0)
    expenses = sum(_amount(r) for r in pnl if _amount(r) < 0)

    kws = _load_transfer_keywords()
    inc_by_source  = defaultdict(float)
    inc_txns       = defaultdict(list)
    exp_by_cat     = defaultdict(float)
    exp_cat_txns   = defaultdict(list)
    exp_by_vendor  = defaultdict(float)
    exp_vnd_txns   = defaultdict(list)

    for r in pnl:
        amt = _amount(r)
        if amt > 0:
            key = _vendor_display(r)
            inc_by_source[key] += amt
            inc_txns[key].append(_txn_min(r, kws))
        else:
            cat = r.get("category") or "Uncategorized"
            exp_by_cat[cat] += abs(amt)
            exp_cat_txns[cat].append(_txn_min(r, kws))
            vnd = _vendor_display(r)
            exp_by_vendor[vnd] += abs(amt)
            exp_vnd_txns[vnd].append(_txn_min(r, kws))

    transactions = sorted(rows, key=lambda r: r["date"], reverse=True)

    return {
        "months": months,
        "selected_month": month_filter or "all",
        "income":   round(income, 2),
        "expenses": round(abs(expenses), 2),
        "net":      round(income + expenses, 2),
        "income_by_source":     _bar_items(inc_by_source, inc_txns),
        "expenses_by_category": _bar_items(exp_by_cat,    exp_cat_txns),
        "expenses_by_vendor":   _bar_items(exp_by_vendor, exp_vnd_txns, top=10),
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


def _txn_min(row, kws=None):
    """Minimal transaction dict for bar chart drilldowns.

    `vendor` = raw bank string (used by edit modal for matching).
    `display` = clean name for display (normalises internal transfer noise).
    """
    amt = _amount(row)
    raw = (row.get("vendor_name") or row.get("description", "")).strip()
    if kws:
        desc = row.get("description", "").lower()
        display = "Internal Transfer" if any(k.lower() in desc or k.lower() in raw.lower() for k in kws) else raw
    else:
        display = raw
    return {
        "id":           row.get("transaction_id", ""),
        "date":         row.get("date", ""),
        "vendor":       raw,
        "display":      display,
        "description":  row.get("description", ""),
        "amount":       round(amt, 2),
        "account_type": row.get("account_type", ""),
        "category":     row.get("category", ""),
        "subcategory":  row.get("subcategory", ""),
    }


def _bar_items(amount_dict, txn_dict, top=None):
    """Build sorted list of {key, amount, transactions} dicts for drilldowns."""
    items = sorted(amount_dict.items(), key=lambda x: x[1], reverse=True)
    if top:
        items = items[:top]
    return [
        {
            "key":          k,
            "amount":       round(v, 2),
            "transactions": sorted(txn_dict.get(k, []), key=lambda t: t["date"], reverse=True),
        }
        for k, v in items
    ]


def get_ledger(month_filter=None, account_type_filter=None, search=None):
    """Return all transactions for the Ledger view, with optional filters."""
    rows = _read_rows(month_filter=month_filter, account_type_filter=account_type_filter)
    months = _available_months(_read_rows())

    if search:
        q = search.lower()
        rows = [
            r for r in rows
            if q in r.get("description", "").lower()
            or q in r.get("vendor_name", "").lower()
            or q in r.get("category", "").lower()
            or q in r.get("bank_name", "").lower()
        ]

    transactions = sorted(rows, key=lambda r: r.get("date", ""), reverse=True)
    return {
        "months":           months,
        "selected_month":   month_filter or "all",
        "transactions":     transactions,
        "total_count":      len(transactions),
    }


def detect_passthrough_pairs(rows, tolerance=None, window_days=None):
    """Find (incoming, outgoing) personal transaction pairs where amounts match
    within `tolerance` and dates are within `window_days` of each other.

    These are candidates for pass-through / mediary transactions that should be
    excluded from P&L (the account just forwarded the money).

    Returns a list of {"in": row, "out": row} dicts, greedily matched by
    smallest time-then-amount distance. Each row appears in at most one pair.
    """
    from datetime import datetime
    from config import PASSTHROUGH_TOLERANCE, PASSTHROUGH_WINDOW_DAYS
    if tolerance   is None: tolerance   = PASSTHROUGH_TOLERANCE
    if window_days is None: window_days = PASSTHROUGH_WINDOW_DAYS

    candidates = list(rows)

    incoming = [r for r in candidates if _amount(r) > 0]
    outgoing = [r for r in candidates if _amount(r) < 0]

    used_out = set()
    pairs    = []

    for inc in incoming:
        inc_amt = _amount(inc)
        try:
            inc_date = datetime.strptime(inc["date"], "%Y-%m-%d")
        except (ValueError, KeyError):
            continue

        best      = None
        best_dist = float("inf")

        for j, out in enumerate(outgoing):
            if j in used_out:
                continue
            out_amt = abs(_amount(out))
            try:
                out_date = datetime.strptime(out["date"], "%Y-%m-%d")
            except (ValueError, KeyError):
                continue

            amt_diff  = abs(inc_amt - out_amt)
            date_diff = abs((inc_date - out_date).days)

            if amt_diff <= tolerance and date_diff <= window_days:
                # Skip cross-account-type pairs: business OUT + personal IN is a
                # salary draw (real personal income), not a pass-through
                if inc.get("account_type") != out.get("account_type"):
                    continue
                dist = date_diff * 2 + amt_diff   # prioritise closeness in time
                if dist < best_dist:
                    best_dist = dist
                    best      = (j, out)

        if best:
            j, out = best
            used_out.add(j)
            pairs.append({"in": inc, "out": out})

    return pairs


def _load_transfer_keywords():
    """Load internal transfer keywords from transfer_config.json.

    These keywords identify transactions that represent internal account
    transfers (e.g. chequing → credit card). Used by scan_transactions()
    to distinguish supplemental income from true pass-throughs.

    Falls back to hardcoded defaults if the file is missing or unreadable.
    """
    from config import TRANSFER_CONFIG_JSON
    defaults = ["INTERNET TRANSFER", "PAYMENT THANK YOU"]
    path = Path(TRANSFER_CONFIG_JSON)
    if not path.exists():
        return defaults
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("internal_transfer_keywords", defaults)
    except Exception as e:
        log.warning("Could not load transfer_config.json: %s", e)
        return defaults


def scan_transactions(rows, tolerance=None, window_days=None):
    """Three-pass transaction scanner for supplemental income and pass-throughs.

    Pass 1 — Internal pairs (Leg 2 + Leg 3):
      Outgoing with internal keyword matched against incoming with internal keyword.
      Example: INTERNET TRANSFER out matched with PAYMENT THANK YOU in.
      Both should be marked Pass-through (internal account transfer).

    Pass 2 — Supplemental income (Leg 1):
      For each internal pair, search for an external incoming transaction (no
      internal keywords) matching Leg 2 by amount and time. This is the real
      supplemental income — money received from an external person that was
      subsequently forwarded internally to pay a credit card.

    Pass 3 — Legacy pass-through pairs:
      Remaining external IN+OUT pairs not consumed by the above passes.
      Both sides marked Pass-through.

    Returns:
      dict with keys:
        internal_pairs    — list of {"in": leg3_row, "out": leg2_row}
        supplemental      — list of individual leg1 rows
        passthrough_pairs — list of {"in": row, "out": row}
    """
    from datetime import datetime as dt
    from config import PASSTHROUGH_TOLERANCE, PASSTHROUGH_WINDOW_DAYS
    if tolerance   is None: tolerance   = PASSTHROUGH_TOLERANCE
    if window_days is None: window_days = PASSTHROUGH_WINDOW_DAYS

    keywords = _load_transfer_keywords()
    kw_lower = [k.lower() for k in keywords]

    def is_internal(row):
        if not kw_lower:
            return False
        desc = row.get("description", "").lower()
        return any(k in desc for k in kw_lower)

    def parse_date(row):
        try:
            return dt.strptime(row["date"], "%Y-%m-%d")
        except (ValueError, KeyError):
            return None

    # Bucket all rows by direction and internal/external nature.
    # Skip rows already classified by a rule or manual edit from the main buckets
    # so the scanner doesn't re-surface already-processed transactions.
    # Exception: already-classified internal outgoing rows are kept separately as
    # anchors for Pass 2 — their Leg 1 (supplemental income) may still be unclassified
    # even though Leg 2+3 were already marked as pass-through in a prior scan.
    internal_out, internal_in, external_in, external_out = [], [], [], []
    classified_internal_out = []   # Leg 2 anchors from previous scans
    # Already-classified Pass-through rows that may need re-classification as owner's draw
    pt_internal_out, pt_internal_in = [], []

    for r in rows:
        amt = _amount(r)
        if amt == 0:
            continue
        already = r.get("categorized_by") in ("rule", "manual")
        if already:
            # Preserve already-classified internal outgoing rows as Pass 2 anchors
            if is_internal(r) and amt < 0:
                classified_internal_out.append(r)
            # Collect existing Pass-through + internal rows for retrospective owner's draw check
            if is_internal(r) and r.get("category") == "Pass-through":
                (pt_internal_in if amt > 0 else pt_internal_out).append(r)
            continue
        if is_internal(r):
            (internal_in if amt > 0 else internal_out).append(r)
        else:
            (external_in if amt > 0 else external_out).append(r)

    # ── Pass 1: Match Leg2 (internal out) with Leg3 (internal in) ───────────
    used_iout = set()
    used_iin  = set()
    internal_pairs = []

    for j, out in enumerate(internal_out):
        oamt  = abs(_amount(out))
        odate = parse_date(out)
        if odate is None:
            continue
        best = None
        best_dist = float("inf")
        for i, inc in enumerate(internal_in):
            if i in used_iin:
                continue
            iamt  = _amount(inc)
            idate = parse_date(inc)
            if idate is None:
                continue
            ad = abs(iamt - oamt)
            dd = abs((idate - odate).days)
            if ad <= tolerance and dd <= window_days:
                dist = dd * 2 + ad
                if dist < best_dist:
                    best_dist = dist
                    best = i
        if best is not None:
            used_iout.add(j)
            used_iin.add(best)
            internal_pairs.append({"out": internal_out[j], "in": internal_in[best]})

    # Split internal pairs into same-account (true internal transfer) and
    # cross-account (business→personal = owner's draw, personal→business = rare).
    same_account_pairs = []
    owner_draw_pairs   = []
    for pair in internal_pairs:
        out_acct = pair["out"].get("account_type", "")
        in_acct  = pair["in"].get("account_type", "")
        if out_acct != in_acct:
            owner_draw_pairs.append(pair)
        else:
            same_account_pairs.append(pair)
    internal_pairs = same_account_pairs   # only same-account pairs stay as pass-through

    # ── Retrospective pass: find cross-account pairs already marked Pass-through ──
    # These are business→personal (or vice-versa) INTERNET TRANSFER rows that were
    # classified as Pass-through in a prior scan before owner's draw logic existed.
    used_pt_out = set()
    used_pt_in  = set()
    for j, out in enumerate(pt_internal_out):
        oamt  = abs(_amount(out))
        odate = parse_date(out)
        if odate is None:
            continue
        best = None
        best_dist = float("inf")
        for i, inc in enumerate(pt_internal_in):
            if i in used_pt_in:
                continue
            if inc.get("account_type") == out.get("account_type"):
                continue   # same account type = genuine internal transfer, leave it
            iamt  = _amount(inc)
            idate = parse_date(inc)
            if idate is None:
                continue
            ad = abs(iamt - oamt)
            dd = abs((idate - odate).days)
            if ad <= tolerance and dd <= window_days:
                dist = dd * 2 + ad
                if dist < best_dist:
                    best_dist = dist
                    best = i
        if best is not None:
            used_pt_out.add(j)
            used_pt_in.add(best)
            owner_draw_pairs.append({"out": pt_internal_out[j], "in": pt_internal_in[best]})

    # ── Pass 2: Find Leg1 for each internal pair ─────────────────────────────
    # Anchors = Leg 2 rows from:
    #   a) newly found internal pairs (unclassified this scan)
    #   b) already-classified internal outgoing rows from prior scans
    # This means if you've already marked Leg 2+3 as pass-through but haven't
    # yet confirmed Leg 1 as supplemental income, it still surfaces here.
    leg2_anchors = [pair["out"] for pair in internal_pairs] + classified_internal_out

    used_ein_sup = set()
    supplemental = []

    for leg2 in leg2_anchors:
        l2amt  = abs(_amount(leg2))
        l2date = parse_date(leg2)
        if l2date is None:
            continue
        best = None
        best_dist = float("inf")
        for i, ext in enumerate(external_in):
            if i in used_ein_sup:
                continue
            eamt  = _amount(ext)
            edate = parse_date(ext)
            if edate is None:
                continue
            ad = abs(eamt - l2amt)
            dd = abs((edate - l2date).days)
            if ad <= tolerance and dd <= window_days:
                dist = dd * 2 + ad
                if dist < best_dist:
                    best_dist = dist
                    best = i
        if best is not None:
            used_ein_sup.add(best)
            supplemental.append(external_in[best])

    # ── Pass 3: Legacy external pass-through pairs ───────────────────────────
    remaining = (
        [r for i, r in enumerate(external_in)  if i not in used_ein_sup]
        + list(external_out)
    )
    passthrough_pairs = detect_passthrough_pairs(remaining, tolerance, window_days)

    log.info(
        "scan_transactions: %d internal pairs, %d owner's draw pairs, %d supplemental, %d pass-through pairs",
        len(internal_pairs), len(owner_draw_pairs), len(supplemental), len(passthrough_pairs),
    )
    return {
        "internal_pairs":    internal_pairs,
        "owner_draw_pairs":  owner_draw_pairs,
        "supplemental":      supplemental,
        "passthrough_pairs": passthrough_pairs,
    }


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
