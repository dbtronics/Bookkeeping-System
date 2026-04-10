#!/usr/bin/env python3
"""
test.py — Bookkeeping System test suite.
Run with: python test.py

Tests cover config, imports, file structure, CSV utilities, rules engine,
NL query builder, and Flask app health. No real Claude API calls are made.
"""

import os
import sys
import json
import tempfile
import traceback
from pathlib import Path

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

PASS  = "\033[92m  ✓\033[0m"   # green
FAIL  = "\033[91m  ✗\033[0m"   # red
WARN  = "\033[93m  !\033[0m"   # yellow
HEAD  = "\033[1m"               # bold
RESET = "\033[0m"

results = {"passed": 0, "failed": 0, "warned": 0}


def passed(label):
    print(f"{PASS} {label}")
    results["passed"] += 1


def failed(label, reason=""):
    msg = f"{FAIL} {label}"
    if reason:
        msg += f"\n       → {reason}"
    print(msg)
    results["failed"] += 1


def warned(label, reason=""):
    msg = f"{WARN} {label}"
    if reason:
        msg += f"\n       → {reason}"
    print(msg)
    results["warned"] += 1


def section(title):
    print(f"\n{HEAD}[{title}]{RESET}")


def run(label, fn):
    """Run a test function. Catches exceptions so one failure doesn't stop the rest."""
    try:
        result = fn()
        if result is False:
            failed(label)
        elif isinstance(result, str):   # string = warning message
            warned(label, result)
        else:
            passed(label)
    except AssertionError as e:
        failed(label, str(e))
    except Exception as e:
        failed(label, f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------------------
# SECTION: Imports
# ---------------------------------------------------------------------------

section("IMPORTS")

def _import(mod):
    def _():
        __import__(mod)
    return _

run("config",               _import("config"))
run("logger",               _import("logger"))
run("csv_utils",            _import("csv_utils"))
run("categorizer",          _import("categorizer"))
run("raw_processor",        _import("raw_processor"))
run("dashboard.aggregator", _import("dashboard.aggregator"))
run("dashboard.routes",     _import("dashboard.routes"))
run("query.nl",             _import("query.nl"))
run("app",                  _import("app"))


# ---------------------------------------------------------------------------
# SECTION: Config values
# ---------------------------------------------------------------------------

section("CONFIG")

import config

def _env_vars():
    missing = []
    for var in ["ANTHROPIC_API_KEY", "DASHBOARD_PASSWORD", "FLASK_SECRET_KEY", "NEXTCLOUD_BASE"]:
        if not os.environ.get(var):
            missing.append(var)
    if missing:
        raise AssertionError(f"Missing in .env: {', '.join(missing)}")

def _secret_key_not_default():
    if config.FLASK_SECRET_KEY == "change-me":
        return "FLASK_SECRET_KEY is still the default 'change-me' — set a real value in .env"

def _confidence_threshold():
    assert 0.0 < config.CONFIDENCE_THRESHOLD <= 1.0, \
        f"CONFIDENCE_THRESHOLD must be between 0 and 1, got {config.CONFIDENCE_THRESHOLD}"

def _nl_history_limit():
    assert isinstance(config.NL_HISTORY_LIMIT, int) and config.NL_HISTORY_LIMIT > 0, \
        f"NL_HISTORY_LIMIT must be a positive int, got {config.NL_HISTORY_LIMIT}"

def _nl_models():
    assert len(config.NL_MODELS) >= 1, "NL_MODELS must have at least one entry"
    for m in config.NL_MODELS:
        assert "id" in m and "label" in m, f"NL_MODELS entry missing id or label: {m}"

def _default_model_in_list():
    ids = {m["id"] for m in config.NL_MODELS}
    assert config.NL_DEFAULT_MODEL in ids, \
        f"NL_DEFAULT_MODEL '{config.NL_DEFAULT_MODEL}' not in NL_MODELS list"

def _categories_not_empty():
    assert config.BUSINESS_CATEGORIES, "BUSINESS_CATEGORIES is empty"
    assert config.PERSONAL_CATEGORIES, "PERSONAL_CATEGORIES is empty"
    assert "Uncategorized" in config.BUSINESS_CATEGORIES, "BUSINESS_CATEGORIES missing 'Uncategorized'"
    assert "Uncategorized" in config.PERSONAL_CATEGORIES, "PERSONAL_CATEGORIES missing 'Uncategorized'"

def _haiku_cost():
    assert isinstance(config.HAIKU_COST_PER_CALL, float) and config.HAIKU_COST_PER_CALL > 0, \
        f"HAIKU_COST_PER_CALL must be a positive float, got {config.HAIKU_COST_PER_CALL}"

run("All required .env vars present",           _env_vars)
run("FLASK_SECRET_KEY is not default",          _secret_key_not_default)
run("CONFIDENCE_THRESHOLD is valid (0–1)",      _confidence_threshold)
run("NL_HISTORY_LIMIT is a positive int",       _nl_history_limit)
run("NL_MODELS list is well-formed",            _nl_models)
run("NL_DEFAULT_MODEL is in NL_MODELS",         _default_model_in_list)
run("Category lists are non-empty with fallback", _categories_not_empty)
run("HAIKU_COST_PER_CALL is a positive float",  _haiku_cost)


# ---------------------------------------------------------------------------
# SECTION: File structure
# ---------------------------------------------------------------------------

section("FILE STRUCTURE")

def _path_exists(p, label):
    def _():
        if not Path(p).exists():
            raise AssertionError(f"Not found: {p}")
    return _

def _nextcloud_base():
    p = config.NEXTCLOUD_BASE
    if not p.exists():
        raise AssertionError(f"NEXTCLOUD_BASE does not exist: {p}")

def _rules_json():
    if not config.RULES_JSON.exists():
        return f"rules.json not found at {config.RULES_JSON} — create it per README Step 4"

def _master_csv():
    if not config.MASTER_TRANSACTIONS_CSV.exists():
        return f"master_transactions.csv not found — run raw_processor.py to generate it"

def _raw_dir():
    raw = config.NEXTCLOUD_BASE / "bank-transactions" / "raw"
    if not raw.exists():
        return f"bank-transactions/raw/ not found — create it per README Step 3"

# Project files that must always be present
for fname in ["app.py", "config.py", "categorizer.py", "csv_utils.py",
              "raw_processor.py", "logger.py", "run.sh", "requirements.txt"]:
    run(f"{fname} exists", _path_exists(Path(__file__).parent / fname, fname))

for dname in ["templates", "static", "dashboard", "query", "ingest"]:
    run(f"{dname}/ directory exists", _path_exists(Path(__file__).parent / dname, dname))

run("NEXTCLOUD_BASE exists",           _nextcloud_base)
run("rules.json exists",               _rules_json)
run("master_transactions.csv exists",  _master_csv)
run("bank-transactions/raw/ exists",   _raw_dir)


# ---------------------------------------------------------------------------
# SECTION: CSV utilities
# ---------------------------------------------------------------------------

section("CSV UTILITIES")

from csv_utils import (
    TRANSACTION_HEADERS, ensure_csv, read_csv, append_row,
    load_transaction_state, is_duplicate, register_transaction,
)

def _ensure_csv_creates_file():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
        tmp = Path(tf.name)
    tmp.unlink()   # delete so ensure_csv has to create it
    try:
        ensure_csv(tmp, ["col_a", "col_b"])
        assert tmp.exists(), "File was not created"
        rows = read_csv(tmp)
        assert rows == [], "New file should have no data rows"
    finally:
        tmp.unlink(missing_ok=True)

def _append_and_read():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
        tmp = Path(tf.name)
    tmp.unlink()  # remove so ensure_csv creates it fresh with headers
    try:
        ensure_csv(tmp, ["date", "amount", "description"])
        append_row(tmp, ["date", "amount", "description"],
                   {"date": "2026-01-15", "amount": "-50.00", "description": "Test"})
        rows = read_csv(tmp)
        assert len(rows) == 1, f"Expected 1 row, got {len(rows)}"
        assert rows[0]["date"] == "2026-01-15"
        assert rows[0]["amount"] == "-50.00"
    finally:
        tmp.unlink(missing_ok=True)

def _dedup_detection():
    with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as tf:
        tmp = Path(tf.name)
    tmp.unlink()  # remove so ensure_csv creates it fresh with headers
    try:
        ensure_csv(tmp, TRANSACTION_HEADERS)
        # Write a row
        row = {h: "" for h in TRANSACTION_HEADERS}
        row.update({"transaction_id": "TXN-20260115-0001", "date": "2026-01-15",
                    "description": "GROCERY STORE", "amount": "-42.00",
                    "bank_name": "CIBC", "account_type": "personal", "card_type": "credit"})
        append_row(tmp, TRANSACTION_HEADERS, row)
        keys, counter = load_transaction_state(tmp)
        assert is_duplicate("2026-01-15", "GROCERY STORE", "-42.00",
                            "CIBC", "personal", "credit", keys), \
            "Should detect existing row as duplicate"
        assert not is_duplicate("2026-01-15", "GROCERY STORE", "-99.00",
                               "CIBC", "personal", "credit", keys), \
            "Different amount should not be a duplicate"
    finally:
        tmp.unlink(missing_ok=True)

def _register_transaction_id():
    keys, counter = set(), {}
    txn_id = register_transaction(
        "2026-01-15", "GROCERY STORE", "-42.00",
        "CIBC", "personal", "credit", keys, counter
    )
    assert txn_id == "TXN-20260115-0001", f"Expected TXN-20260115-0001, got {txn_id}"
    txn_id2 = register_transaction(
        "2026-01-15", "PHARMACY", "-12.50",
        "CIBC", "personal", "credit", keys, counter
    )
    assert txn_id2 == "TXN-20260115-0002", f"Expected TXN-20260115-0002, got {txn_id2}"

def _transaction_headers_complete():
    required = ["transaction_id", "date", "description", "amount",
                "bank_name", "account_type", "card_type",
                "category", "categorized_by", "flagged", "exclude_from_pnl"]
    missing = [f for f in required if f not in TRANSACTION_HEADERS]
    assert not missing, f"TRANSACTION_HEADERS missing: {missing}"

run("ensure_csv creates file with headers",     _ensure_csv_creates_file)
run("append_row + read_csv round-trip",         _append_and_read)
run("is_duplicate detects existing row",        _dedup_detection)
run("register_transaction generates TXN IDs",  _register_transaction_id)
run("TRANSACTION_HEADERS has all required fields", _transaction_headers_complete)


# ---------------------------------------------------------------------------
# SECTION: Rules engine
# ---------------------------------------------------------------------------

section("RULES ENGINE")

from categorizer import load_rules, match_rule, archive_rules

SAMPLE_RULES = [
    {
        "id": "rule-001",
        "description": "GoHighLevel — SaaS CRM",
        "match": {"vendor_name_contains": "gohighlevel", "account_type": "business"},
        "apply": {"category": "SaaS tools", "subcategory": "CRM", "exclude_from_pnl": False}
    },
    {
        "id": "rule-002",
        "description": "Personal rent transfer",
        "match": {"vendor_name_contains": "rent transfer", "account_type": "personal"},
        "apply": {"category": "Remittances", "exclude_from_pnl": True}
    },
]

def _load_rules_returns_list():
    rules = load_rules()
    assert isinstance(rules, list), f"load_rules() should return a list, got {type(rules)}"

def _match_rule_hits():
    txn = {"description": "GOHIGHLEVEL.COM SUBSCRIPTION", "account_type": "business"}
    apply, rule_id, _ = match_rule(txn, SAMPLE_RULES)
    assert apply is not None, "Should have matched rule-001"
    assert rule_id == "rule-001"
    assert apply["category"] == "SaaS tools"

def _match_rule_case_insensitive():
    txn = {"description": "GoHighLevel Monthly Fee", "account_type": "business"}
    apply, rule_id, _ = match_rule(txn, SAMPLE_RULES)
    assert apply is not None, "Match should be case-insensitive"

def _match_rule_wrong_account_type():
    txn = {"description": "gohighlevel payment", "account_type": "personal"}
    apply, rule_id, _ = match_rule(txn, SAMPLE_RULES)
    assert apply is None, "rule-001 is business-only, should not match personal"

def _match_rule_no_match():
    txn = {"description": "UNKNOWN VENDOR 12345", "account_type": "business"}
    apply, rule_id, _ = match_rule(txn, SAMPLE_RULES)
    assert apply is None, "Should return None when no rule matches"

def _match_rule_first_wins():
    # Both rules could match if keywords overlap — first in list must win
    overlap_rules = [
        {"id": "first",  "description": "First", "match": {"vendor_name_contains": "test"}, "apply": {"category": "A"}},
        {"id": "second", "description": "Second","match": {"vendor_name_contains": "test"}, "apply": {"category": "B"}},
    ]
    txn = {"description": "test vendor", "account_type": "business"}
    apply, rule_id, _ = match_rule(txn, overlap_rules)
    assert rule_id == "first", f"First matching rule should win, got {rule_id}"

def _archive_rules_no_crash_when_missing():
    # Should return None gracefully, not raise.
    # Patch categorizer.RULES_JSON directly — it was imported by value at load time,
    # so patching config.RULES_JSON alone has no effect.
    import categorizer
    original = categorizer.RULES_JSON
    try:
        categorizer.RULES_JSON = Path("/tmp/nonexistent_rules_test.json")
        result = archive_rules()
        assert result is None
    finally:
        categorizer.RULES_JSON = original

run("load_rules() returns a list",                  _load_rules_returns_list)
run("match_rule() matches correct rule",            _match_rule_hits)
run("match_rule() is case-insensitive",             _match_rule_case_insensitive)
run("match_rule() respects account_type",           _match_rule_wrong_account_type)
run("match_rule() returns None on no match",        _match_rule_no_match)
run("match_rule() first-match-wins ordering",       _match_rule_first_wins)
run("archive_rules() handles missing file safely",  _archive_rules_no_crash_when_missing)


# ---------------------------------------------------------------------------
# SECTION: NL query — summary builder
# ---------------------------------------------------------------------------

section("NL QUERY")

from query.nl import _build_summary

def _build_summary_no_file():
    # Should return a plain string, not crash, when CSV is absent
    import config as cfg
    original = cfg.MASTER_TRANSACTIONS_CSV
    try:
        cfg.MASTER_TRANSACTIONS_CSV = Path("/tmp/nonexistent_master.csv")
        result = _build_summary("all")
        assert isinstance(result, str), "Should return a string"
        assert len(result) > 0, "Should return a non-empty string"
    finally:
        cfg.MASTER_TRANSACTIONS_CSV = original

def _build_summary_with_data():
    # Write a minimal CSV and check the summary contains expected sections
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, newline="") as tf:
        tmp = Path(tf.name)
        import csv as csv_mod
        from csv_utils import TRANSACTION_HEADERS
        writer = csv_mod.DictWriter(tf, fieldnames=TRANSACTION_HEADERS)
        writer.writeheader()
        base = {h: "" for h in TRANSACTION_HEADERS}
        writer.writerow({**base, "date": "2026-01-15", "description": "FYNITE CORP EFT",
                         "vendor_name": "Fynite Corp", "amount": "5000.00",
                         "account_type": "business", "card_type": "chequing",
                         "bank_name": "CIBC", "category": "Revenue",
                         "exclude_from_pnl": "False", "flagged": "False"})
        writer.writerow({**base, "date": "2026-01-20", "description": "SHOPIFY BILLING",
                         "vendor_name": "Shopify", "amount": "-99.00",
                         "account_type": "business", "card_type": "credit",
                         "bank_name": "CIBC", "category": "SaaS tools",
                         "exclude_from_pnl": "False", "flagged": "False"})
    import config as cfg
    original = cfg.MASTER_TRANSACTIONS_CSV
    try:
        cfg.MASTER_TRANSACTIONS_CSV = tmp
        result = _build_summary("all")
        assert "MONTHLY BREAKDOWN" in result, "Summary missing MONTHLY BREAKDOWN"
        assert "BY CATEGORY" in result,       "Summary missing BY CATEGORY"
        assert "TOP VENDORS" in result,       "Summary missing TOP VENDORS"
        assert "2026-01" in result,           "Summary missing the month 2026-01"
    finally:
        cfg.MASTER_TRANSACTIONS_CSV = original
        tmp.unlink(missing_ok=True)

def _build_summary_scope_filter():
    with tempfile.NamedTemporaryFile(suffix=".csv", mode="w", delete=False, newline="") as tf:
        tmp = Path(tf.name)
        import csv as csv_mod
        from csv_utils import TRANSACTION_HEADERS
        writer = csv_mod.DictWriter(tf, fieldnames=TRANSACTION_HEADERS)
        writer.writeheader()
        base = {h: "" for h in TRANSACTION_HEADERS}
        for acct, vendor in [("business", "Biz Vendor"), ("personal", "Personal Vendor")]:
            writer.writerow({**base, "date": "2026-01-10", "description": vendor,
                             "vendor_name": vendor, "amount": "-50.00",
                             "account_type": acct, "card_type": "credit",
                             "bank_name": "CIBC", "category": "Uncategorized",
                             "exclude_from_pnl": "False", "flagged": "False"})
    import config as cfg
    original = cfg.MASTER_TRANSACTIONS_CSV
    try:
        cfg.MASTER_TRANSACTIONS_CSV = tmp
        biz = _build_summary("business")
        assert "BOOKKEEPING SUMMARY (BUSINESS)" in biz
        per = _build_summary("personal")
        assert "BOOKKEEPING SUMMARY (PERSONAL)" in per
    finally:
        cfg.MASTER_TRANSACTIONS_CSV = original
        tmp.unlink(missing_ok=True)

run("_build_summary() handles missing CSV gracefully", _build_summary_no_file)
run("_build_summary() produces expected sections",     _build_summary_with_data)
run("_build_summary() filters by scope correctly",     _build_summary_scope_filter)


# ---------------------------------------------------------------------------
# SECTION: Flask app
# ---------------------------------------------------------------------------

section("FLASK APP")

def _flask_health():
    from app import app
    with app.test_client() as client:
        resp = client.get("/health")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
        data = resp.get_json()
        assert data.get("status") == "ok", f"Expected {{status: ok}}, got {data}"

def _flask_login_get():
    from app import app
    with app.test_client() as client:
        resp = client.get("/login")
        assert resp.status_code == 200, f"Login page returned {resp.status_code}"

def _flask_login_wrong_password():
    from app import app
    import config as cfg
    if cfg.AUTO_LOGIN:
        return "AUTO_LOGIN=true — login check skipped"
    with app.test_client() as client:
        resp = client.post("/login", data={"password": "definitely_wrong_password_xyz"})
        assert resp.status_code == 200, \
            "Wrong password should re-render login (200), not redirect"
        assert b"Incorrect" in resp.data, "Error message not found in response"

def _flask_dashboard_requires_auth():
    from app import app
    import config as cfg
    if cfg.AUTO_LOGIN:
        return "AUTO_LOGIN=true — auth guard check skipped"
    with app.test_client() as client:
        resp = client.get("/dashboard")
        assert resp.status_code == 302, \
            f"Unauthenticated /dashboard should redirect (302), got {resp.status_code}"

def _flask_blueprints_registered():
    from app import app
    routes = [str(r) for r in app.url_map.iter_rules()]
    for expected in ["/health", "/login", "/query", "/dashboard", "/business", "/personal"]:
        assert any(expected in r for r in routes), f"Route {expected} not registered"

run("/health returns 200 with {status: ok}",       _flask_health)
run("Login page loads (GET /login → 200)",         _flask_login_get)
run("Wrong password re-renders with error",        _flask_login_wrong_password)
run("Unauthenticated /dashboard redirects",        _flask_dashboard_requires_auth)
run("All expected blueprints registered",          _flask_blueprints_registered)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

total = results["passed"] + results["failed"] + results["warned"]
print(f"\n{'='*54}")
print(f"  {HEAD}Results{RESET}: "
      f"\033[92m{results['passed']} passed\033[0m  "
      f"\033[93m{results['warned']} warnings\033[0m  "
      f"\033[91m{results['failed']} failed\033[0m  "
      f"({total} total)")
print(f"{'='*54}\n")

sys.exit(1 if results["failed"] > 0 else 0)
