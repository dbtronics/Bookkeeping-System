import datetime
import json
import logging
import threading
from functools import wraps
from pathlib import Path

import anthropic
from flask import Blueprint, session, redirect, url_for, render_template, request, jsonify

from config import (
    RULES_JSON, RULES_ARCHIVE_DIR, TRANSFER_CONFIG_JSON,
    ANTHROPIC_API_KEY, HAIKU_MODEL, SONNET_MODEL,
    NL_MODELS, NL_DEFAULT_MODEL, NL_MODEL_PRICING, USD_TO_CAD,
    PASSTHROUGH_TOLERANCE, PASSTHROUGH_WINDOW_DAYS,
    MASTER_TRANSACTIONS_CSV,
)
from settings_utils import (
    load_settings, save_settings,
    get_account_types, get_categories, get_exclude_from_pnl_categories,
)
from dashboard.aggregator import get_overview, get_business, get_personal, get_flagged, get_ledger, detect_passthrough_pairs, scan_transactions

# Shared state for the recategorize background job (single-user app)
_recategorize_state = {}
_recategorize_lock  = threading.Lock()

# Shared state for the raw-file processing job
_process_state  = {}
_process_lock   = threading.Lock()
_process_thread = None   # tracked so we can detect stale "running" state after a page refresh

dashboard_bp = Blueprint("dashboard", __name__)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth guard
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Helper: load rules safely
# ---------------------------------------------------------------------------

def _load_rules():
    path = Path(RULES_JSON)
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data.get("rules", [])
    except Exception as e:
        log.error("Failed to load rules.json: %s", e)
        return []


def _load_suggested_rules():
    path = Path(RULES_ARCHIVE_DIR) / "rules_suggested.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        raw = data.get("suggestions", [])
        # Transform flat suggestion dicts into the match/apply shape the
        # template and approve endpoint both expect
        result = []
        for s in raw:
            vendor   = s.get("vendor_name", "")
            account  = s.get("account_type", "")
            category = s.get("category", "")
            subcat   = s.get("subcategory", "")
            result.append({
                "description": f"{vendor} → {category}" + (f" / {subcat}" if subcat else ""),
                "match": {
                    "vendor_name_contains": vendor,
                    **({"account_type": account} if account else {}),
                },
                "apply": {
                    "category":   category,
                    **({"subcategory": subcat} if subcat else {}),
                },
                "seen_count":   s.get("seen_count", 0),
                "example_desc": s.get("example_desc", ""),
            })
        return result
    except Exception as e:
        log.error("Failed to load rules_suggested.json: %s", e)
        return []


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
@login_required
def dashboard_overview():
    month = request.args.get("month")
    data = get_overview(month_filter=month)
    return render_template("overview.html", **data)


@dashboard_bp.route("/business")
@login_required
def dashboard_business():
    month = request.args.get("month")
    data = get_business(month_filter=month)
    cats = get_categories()
    return render_template("business.html",
                           business_categories=cats.get("business", []),
                           personal_categories=cats.get("personal", []),
                           **data)


@dashboard_bp.route("/personal")
@login_required
def dashboard_personal():
    month = request.args.get("month")
    data = get_personal(month_filter=month)
    cats = get_categories()
    return render_template("personal.html",
                           business_categories=cats.get("business", []),
                           personal_categories=cats.get("personal", []),
                           **data)


@dashboard_bp.route("/flagged")
@login_required
def dashboard_flagged():
    month = request.args.get("month")
    data = get_flagged(month_filter=month)
    return render_template("flagged.html", **data)


@dashboard_bp.route("/ledger")
@login_required
def dashboard_ledger():
    month       = request.args.get("month")
    account     = request.args.get("account")
    search      = request.args.get("q", "").strip()
    data = get_ledger(month_filter=month, account_type_filter=account or None, search=search or None)
    cats = get_categories()
    return render_template(
        "ledger.html",
        business_categories=cats.get("business", []),
        personal_categories=cats.get("personal", []),
        search=search,
        account_filter=account or "",
        **data,
    )


@dashboard_bp.route("/rules")
@login_required
def dashboard_rules():
    rules = _load_rules()
    suggested = _load_suggested_rules()
    cfg = _load_transfer_config()
    cats = get_categories()
    settings = load_settings()
    return render_template(
        "rules.html",
        rules=rules,
        suggested=suggested,
        business_categories=cats.get("business", []),
        personal_categories=cats.get("personal", []),
        transfer_keywords=cfg.get("internal_transfer_keywords", []),
        settings_account_types=settings.get("account_types", []),
        settings_categories=cats,
        exclude_from_pnl_categories=list(settings.get("exclude_from_pnl_categories", [])),
    )


# ---------------------------------------------------------------------------
# Helper: apply a single rule to matching rows in master_transactions.csv
# ---------------------------------------------------------------------------

def _apply_rule_to_master(rule):
    """Update master_transactions.csv rows that match the given rule.

    Skips rows where categorized_by == 'manual' (human overrides are sacred).
    Returns the number of rows updated.
    """
    from config import MASTER_TRANSACTIONS_CSV
    from csv_utils import TRANSACTION_HEADERS
    import csv as csv_mod
    import shutil

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return 0

    match  = rule.get("match", {})
    apply  = rule.get("apply", {})
    keyword      = match.get("vendor_name_contains", "").lower()
    rule_account = match.get("account_type", "")
    rule_sign    = match.get("amount_sign", "")   # "positive" | "negative" | "" (either)

    updated = 0
    rows = []

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv_mod.DictReader(f))

    for row in rows:
        if row.get("categorized_by") == "manual":
            continue
        try:
            row_amount = float(row.get("amount", 0))
        except (ValueError, TypeError):
            row_amount = 0.0
        desc_match    = keyword and keyword in row.get("description", "").lower()
        account_match = not rule_account or rule_account == row.get("account_type", "")
        sign_match    = (
            not rule_sign
            or (rule_sign == "positive" and row_amount > 0)
            or (rule_sign == "negative" and row_amount < 0)
        )
        if desc_match and account_match and sign_match:
            row["category"]        = apply.get("category", row["category"])
            row["subcategory"]     = apply.get("subcategory", row.get("subcategory", ""))
            row["categorized_by"]  = "rule"
            row["confidence"]      = ""
            row["flagged"]         = str(apply.get("flagged", False))
            row["flag_reason"]     = apply.get("flag_reason", "")
            row["exclude_from_pnl"] = str(apply.get("exclude_from_pnl", row.get("exclude_from_pnl", False)))
            updated += 1

    if updated:
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=TRANSACTION_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        shutil.move(str(tmp), str(path))

    return updated


# ---------------------------------------------------------------------------
# Rules API endpoints (used by dashboard JS)
# ---------------------------------------------------------------------------

@dashboard_bp.route("/rules/approve", methods=["POST"])
@login_required
def rules_approve():
    """Approve a suggested rule and add it to rules.json."""
    suggestion = request.get_json()
    if not suggestion:
        return jsonify({"error": "No data"}), 400

    rules_path = Path(RULES_JSON)
    suggested_path = Path(RULES_ARCHIVE_DIR) / "rules_suggested.json"

    try:
        # Archive current rules.json
        from categorizer import archive_rules
        archive_rules()

        # Load current rules
        if rules_path.exists():
            with open(rules_path, encoding="utf-8") as f:
                data = json.load(f)
        else:
            data = {"version": "1.0", "rules": []}

        # Build new rule from suggestion
        existing_ids = {r["id"] for r in data["rules"]}
        rule_num = len(data["rules"]) + 1
        new_id = f"rule-{rule_num:03d}"
        while new_id in existing_ids:
            rule_num += 1
            new_id = f"rule-{rule_num:03d}"

        new_rule = {
            "id": new_id,
            "description": suggestion.get("description", ""),
            "match": suggestion.get("match", {}),
            "apply": suggestion.get("apply", {}),
        }
        data["rules"].append(new_rule)

        import datetime
        data["last_updated"] = datetime.date.today().isoformat()

        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

        # Remove from suggestions — match on vendor+category (the raw stored fields),
        # NOT description (which only exists after _load_suggested_rules transforms them)
        vendor_key   = new_rule["match"].get("vendor_name_contains", "")
        category_key = new_rule["apply"].get("category", "")
        if suggested_path.exists():
            with open(suggested_path, encoding="utf-8") as f:
                sug_data = json.load(f)
            sug_data["suggestions"] = [
                s for s in sug_data.get("suggestions", [])
                if not (
                    s.get("vendor_name", "").lower() == vendor_key.lower()
                    and s.get("category", "") == category_key
                )
            ]
            with open(suggested_path, "w", encoding="utf-8") as f:
                json.dump(sug_data, f, indent=2)

        # Apply the new rule immediately to matching rows in master_transactions.csv
        rows_updated = _apply_rule_to_master(new_rule)

        log.info("Approved rule %s — updated %d existing rows in master CSV", new_id, rows_updated)
        return jsonify({"status": "ok", "rule_id": new_id, "rows_updated": rows_updated})

    except Exception as e:
        log.error("Failed to approve rule: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/rules/recategorize", methods=["POST"])
@login_required
def rules_recategorize():
    """Start a background recategorize job. Returns immediately."""
    global _recategorize_state
    with _recategorize_lock:
        if _recategorize_state.get("running"):
            return jsonify({"error": "Already running"}), 409
        _recategorize_state = {"running": True, "done": False, "processed": 0, "total": 0}

    def _run():
        global _recategorize_state
        try:
            from recategorize import recategorize
            recategorize(progress=_recategorize_state)
        except Exception as e:
            log.error("Recategorize failed: %s", e)
            _recategorize_state["error"] = str(e)
            _recategorize_state["running"] = False
            _recategorize_state["done"] = True

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@dashboard_bp.route("/rules/recategorize/status", methods=["GET"])
@login_required
def rules_recategorize_status():
    """Poll this endpoint for live job progress."""
    return jsonify(dict(_recategorize_state))


@dashboard_bp.route("/process/start", methods=["POST"])
@login_required
def process_start():
    """Start a background raw-file processing job. Returns immediately.

    If a job is still genuinely running, returns {"status": "already_running"}
    so the modal can attach to the existing job and start polling.
    If the thread has died but state was never cleaned up (e.g. server restart,
    page refresh after crash), auto-resets and starts fresh.
    """
    global _process_state, _process_thread
    with _process_lock:
        # Auto-reset stale state: running flag set but thread is gone
        if _process_state.get("running") and _process_thread and not _process_thread.is_alive():
            log.info("Stale process state detected — resetting before new run")
            _process_state = {}

        if _process_state.get("running"):
            # Genuinely still running — let the modal attach and start polling
            return jsonify({"status": "already_running"})

        _process_state = {"running": True, "done": False}

    def _run():
        from raw_processor import run_with_progress
        run_with_progress(_process_state)

    _process_thread = threading.Thread(target=_run, daemon=True)
    _process_thread.start()
    return jsonify({"status": "started"})


@dashboard_bp.route("/process/cancel", methods=["POST"])
@login_required
def process_cancel():
    """Request cancellation of the running processing job.

    Sets a cancel_requested flag that the background thread checks between
    files. Also fires the input_event so the thread unblocks immediately
    if it is currently waiting for user input.
    """
    with _process_lock:
        if not _process_state.get("running"):
            return jsonify({"status": "not_running"})
        _process_state["cancel_requested"] = True
        event = _process_state.get("_input_event")
    if event:
        event.set()   # unblock thread if it's waiting for user input
    return jsonify({"status": "cancelling"})


@dashboard_bp.route("/process/status", methods=["GET"])
@login_required
def process_status():
    """Poll for raw-file processing progress. Strips non-JSON-serializable values."""
    safe = {k: v for k, v in _process_state.items()
            if isinstance(v, (str, int, float, bool, list, dict, type(None)))}
    return jsonify(safe)


@dashboard_bp.route("/process/answer", methods=["POST"])
@login_required
def process_answer():
    """Submit user's identification answer for a file waiting for input."""
    data = request.get_json() or {}
    with _process_lock:
        _process_state["answer"] = data
        event = _process_state.get("_input_event")
    if event:
        event.set()
    return jsonify({"status": "ok"})


@dashboard_bp.route("/rules/dismiss", methods=["POST"])
@login_required
def rules_dismiss():
    """Dismiss a suggested rule without adding it."""
    suggestion = request.get_json()
    suggested_path = Path(RULES_ARCHIVE_DIR) / "rules_suggested.json"
    try:
        if suggested_path.exists():
            with open(suggested_path, encoding="utf-8") as f:
                data = json.load(f)
            data["suggestions"] = [
                s for s in data.get("suggestions", [])
                if s.get("description") != suggestion.get("description")
            ]
            with open(suggested_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("Failed to dismiss suggestion: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Phase 11 — NL rule creation from chat
# ---------------------------------------------------------------------------

def _write_rule_to_json(rule_data):
    """Archive rules.json, append the new rule, return the assigned rule id."""
    rules_path = Path(RULES_JSON)
    from categorizer import archive_rules
    archive_rules()

    if rules_path.exists():
        with open(rules_path, encoding="utf-8") as f:
            data = json.load(f)
    else:
        data = {"version": "1.0", "rules": []}

    existing_ids = {r["id"] for r in data["rules"]}
    rule_num = len(data["rules"]) + 1
    new_id = f"rule-{rule_num:03d}"
    while new_id in existing_ids:
        rule_num += 1
        new_id = f"rule-{rule_num:03d}"

    new_rule = {
        "id":          new_id,
        "description": rule_data.get("description", ""),
        "match":       rule_data.get("match", {}),
        "apply":       rule_data.get("apply", {}),
    }
    data["rules"].append(new_rule)
    data["last_updated"] = datetime.date.today().isoformat()

    with open(rules_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    return new_rule


@dashboard_bp.route("/rules/propose", methods=["POST"])
@login_required
def rules_propose():
    """Parse a natural language rule description and return a rule JSON preview.

    Request:  { "description": "...", "model": "claude-haiku-..." }
    Response: { "can_generate": true,  "rule": {...}, "explanation": "...",
                "tokens": {...}, "cost_usd": ..., "cost_cad": ... }
          or: { "can_generate": false, "explanation": "what info is needed" }
    """
    data    = request.get_json(silent=True) or {}
    desc    = (data.get("description") or "").strip()
    model_id = data.get("model", NL_DEFAULT_MODEL)
    if model_id not in {m["id"] for m in NL_MODELS}:
        model_id = NL_DEFAULT_MODEL
    if not desc:
        return jsonify({"error": "No description provided"}), 400

    cats = get_categories()
    _system_only = get_exclude_from_pnl_categories()
    biz_cats = ", ".join(c for c in cats.get("business", []) if c not in _system_only)
    per_cats = ", ".join(c for c in cats.get("personal", []) if c not in _system_only)

    prompt = f"""You are a rule generator for a household bookkeeping system.
Convert the user's plain-English description into one or more rule JSON objects.

Each rule has this schema:
{{
  "description": "short human-readable label",
  "match": {{
    "vendor_name_contains": "keyword to find in bank description (lowercase)",
    "account_type": "business OR personal — omit if applies to both"
  }},
  "apply": {{
    "category":         "exact category from the valid list below",
    "subcategory":      "optional short label",
    "exclude_from_pnl": false
  }}
}}

Valid business categories: {biz_cats}
Valid personal categories:  {per_cats}

IMPORTANT GUIDELINES:
- If the user names multiple vendors/sources, generate one rule per vendor — do NOT ask them to split the request.
- Be liberal in inferring intent. If the user says "Upwork inbound as revenue", use vendor_name_contains: "upwork" and category: "Revenue".
- Only return can_generate: false if you genuinely cannot determine the category or vendor keyword at all.
- For inbound/income transactions on a business account, the category is almost always "Revenue".
- For wire transfers, use vendor_name_contains: "wire" (or the most distinctive keyword in the description).
- For eTransfer from a named person/company, use the name as the keyword.

If you can generate rules, return ONLY this JSON (no markdown):
{{
  "can_generate": true,
  "explanation":  "one sentence summarising what these rules will do",
  "rules": [ {{ ...rule object... }}, ... ]
}}

If you truly cannot infer enough to write even one rule, return ONLY:
{{
  "can_generate": false,
  "explanation":  "what specific information you still need"
}}

User description: {desc}"""

    try:
        client   = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        response = client.messages.create(
            model=model_id,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]

        result = json.loads(raw)

        # Normalise: accept both "rule" (singular) and "rules" (array)
        if result.get("can_generate") and "rule" in result and "rules" not in result:
            result["rules"] = [result.pop("rule")]

        pricing  = NL_MODEL_PRICING.get(model_id, {"input": 0.80, "output": 4.00})
        tok_in   = response.usage.input_tokens
        tok_out  = response.usage.output_tokens
        cost_usd = (tok_in * pricing["input"] + tok_out * pricing["output"]) / 1_000_000
        cost_cad = cost_usd * USD_TO_CAD

        log.info("Rule proposed via chat | model=%s can_generate=%s rules=%d | %s",
                 model_id, result.get("can_generate"),
                 len(result.get("rules", [])), desc[:60])

        return jsonify({
            **result,
            "tokens":   {"input": tok_in, "output": tok_out},
            "cost_usd": round(cost_usd, 6),
            "cost_cad": round(cost_cad, 6),
        })

    except json.JSONDecodeError as e:
        log.error("Rule propose — Claude returned invalid JSON: %s", e)
        return jsonify({"error": "Claude returned an unexpected response. Try rephrasing."}), 500
    except Exception as e:
        log.error("Rule propose failed: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/rules/save", methods=["POST"])
@login_required
def rules_save():
    """Save a confirmed proposed rule to rules.json and apply it to master CSV.

    Request:  { "rule": { description, match, apply } }
    Response: { "status": "ok", "rule_id": "...", "rows_updated": N }
    """
    data      = request.get_json(silent=True) or {}
    rule_data = data.get("rule")
    if not rule_data or not rule_data.get("match") or not rule_data.get("apply"):
        return jsonify({"error": "Invalid rule data"}), 400

    try:
        new_rule     = _write_rule_to_json(rule_data)
        rows_updated = _apply_rule_to_master(new_rule)
        log.info("Rule saved via chat: %s — updated %d rows", new_rule["id"], rows_updated)
        return jsonify({"status": "ok", "rule_id": new_rule["id"], "rows_updated": rows_updated})
    except Exception as e:
        log.error("Rule save failed: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/rules/update", methods=["POST"])
@login_required
def rules_update():
    """Edit an existing rule in rules.json and re-apply it to master CSV.

    Request:  { "id": "rule-001", "rule": { description, match, apply } }
    Response: { "status": "ok", "rows_updated": N }
    """
    data      = request.get_json(silent=True) or {}
    rule_id   = data.get("id", "").strip()
    rule_data = data.get("rule")
    if not rule_id or not rule_data:
        return jsonify({"error": "Missing id or rule"}), 400

    rules_path = Path(RULES_JSON)
    try:
        from categorizer import archive_rules
        archive_rules()

        with open(rules_path, encoding="utf-8") as f:
            doc = json.load(f)

        matched = False
        for i, r in enumerate(doc["rules"]):
            if r["id"] == rule_id:
                doc["rules"][i] = {
                    "id":          rule_id,
                    "description": rule_data.get("description", ""),
                    "match":       rule_data.get("match", {}),
                    "apply":       rule_data.get("apply", {}),
                }
                updated_rule = doc["rules"][i]
                matched = True
                break

        if not matched:
            return jsonify({"error": f"Rule {rule_id} not found"}), 404

        doc["last_updated"] = datetime.date.today().isoformat()
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)

        rows_updated = _apply_rule_to_master(updated_rule)
        log.info("Rule updated: %s — %d rows re-applied", rule_id, rows_updated)
        return jsonify({"status": "ok", "rows_updated": rows_updated})
    except Exception as e:
        log.error("Rule update failed: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/rules/delete", methods=["POST"])
@login_required
def rules_delete():
    """Remove a rule from rules.json.

    Request:  { "id": "rule-001" }
    Response: { "status": "ok" }
    """
    data    = request.get_json(silent=True) or {}
    rule_id = data.get("id", "").strip()
    if not rule_id:
        return jsonify({"error": "Missing id"}), 400

    rules_path = Path(RULES_JSON)
    try:
        from categorizer import archive_rules
        archive_rules()

        with open(rules_path, encoding="utf-8") as f:
            doc = json.load(f)

        before = len(doc["rules"])
        doc["rules"] = [r for r in doc["rules"] if r["id"] != rule_id]
        if len(doc["rules"]) == before:
            return jsonify({"error": f"Rule {rule_id} not found"}), 404

        doc["last_updated"] = datetime.date.today().isoformat()
        with open(rules_path, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)

        log.info("Rule deleted: %s", rule_id)
        return jsonify({"status": "ok"})
    except Exception as e:
        log.error("Rule delete failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Transaction inline edit
# ---------------------------------------------------------------------------

@dashboard_bp.route("/transactions/update", methods=["POST"])
@login_required
def transactions_update():
    """Edit category/subcategory for one or all similar transactions.

    Request: {
      transaction_id, category, subcategory,
      scope: "single" | "all",
      vendor_name: <cleaned vendor string used for 'all similar' matching>,
      account_type,
      create_rule: bool
    }
    Response: { status, updated, rule_id }
    """
    import csv as csv_mod, shutil
    from csv_utils import TRANSACTION_HEADERS

    data         = request.get_json(silent=True) or {}
    txn_id       = data.get("transaction_id", "").strip()
    category     = data.get("category", "").strip()
    subcategory  = data.get("subcategory", "").strip()
    scope        = data.get("scope", "single")
    vendor_name  = (data.get("vendor_name") or "").strip()
    account_type = data.get("account_type", "").strip()
    create_rule  = bool(data.get("create_rule", False))

    if not txn_id or not category:
        return jsonify({"error": "Missing transaction_id or category"}), 400

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return jsonify({"error": "master_transactions.csv not found"}), 500

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))

        updated = 0
        for row in rows:
            is_target = row.get("transaction_id") == txn_id
            if scope == "all":
                vn_match  = vendor_name and (
                    row.get("vendor_name", "").lower() == vendor_name.lower()
                )
                acct_match = not account_type or row.get("account_type") == account_type
                matches = is_target or (vn_match and acct_match)
            else:
                matches = is_target

            if matches:
                row["category"]       = category
                row["subcategory"]    = subcategory
                # "rule" only when scope=all AND a rule is being created;
                # every other manual edit stays as "manual"
                row["categorized_by"] = "rule" if (scope == "all" and create_rule) else "manual"
                row["flagged"]        = "False"
                row["flag_reason"]    = ""
                # Some categories always imply exclusion from P&L
                if category in get_exclude_from_pnl_categories():
                    row["exclude_from_pnl"] = "True"
                updated += 1

        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=TRANSACTION_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        shutil.move(str(tmp), str(path))

        rule_id = None
        if create_rule and vendor_name:
            keyword = vendor_name.lower()
            rule_data = {
                "description": f"{vendor_name} → {category}" + (f" / {subcategory}" if subcategory else ""),
                "match": {
                    "vendor_name_contains": keyword,
                    **({"account_type": account_type} if account_type else {}),
                },
                "apply": {
                    "category": category,
                    **({"subcategory": subcategory} if subcategory else {}),
                    "exclude_from_pnl": False,
                },
            }
            new_rule = _write_rule_to_json(rule_data)
            rule_id  = new_rule["id"]

        log.info("Transaction edit: scope=%s updated=%d rule=%s", scope, updated, rule_id)
        return jsonify({"status": "ok", "updated": updated, "rule_id": rule_id})

    except Exception as e:
        log.error("Transaction update failed: %s", e)
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Transfer keyword management
# ---------------------------------------------------------------------------

def _load_transfer_config():
    path = Path(TRANSFER_CONFIG_JSON)
    if not path.exists():
        return {"internal_transfer_keywords": ["INTERNET TRANSFER", "PAYMENT THANK YOU"]}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("Failed to load transfer_config.json: %s", e)
        return {"internal_transfer_keywords": []}


def _save_transfer_config(data):
    path = Path(TRANSFER_CONFIG_JSON)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


@dashboard_bp.route("/settings/transfer-keywords", methods=["GET"])
@login_required
def transfer_keywords_get():
    """Return current list of internal transfer keywords."""
    cfg = _load_transfer_config()
    return jsonify({"keywords": cfg.get("internal_transfer_keywords", [])})


@dashboard_bp.route("/settings/transfer-keywords/add", methods=["POST"])
@login_required
def transfer_keywords_add():
    """Add a keyword to the internal transfer list.

    Request:  { "keyword": "INTERNET TRANSFER" }
    Response: { "status": "ok", "keywords": [...] }
    """
    data    = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip().upper()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400
    cfg = _load_transfer_config()
    kws = cfg.get("internal_transfer_keywords", [])
    if keyword not in kws:
        kws.append(keyword)
        cfg["internal_transfer_keywords"] = kws
        _save_transfer_config(cfg)
        log.info("Transfer keyword added: %s", keyword)
    return jsonify({"status": "ok", "keywords": kws})


@dashboard_bp.route("/settings/transfer-keywords/delete", methods=["POST"])
@login_required
def transfer_keywords_delete():
    """Remove a keyword from the internal transfer list.

    Request:  { "keyword": "PAYMENT THANK YOU" }
    Response: { "status": "ok", "keywords": [...] }
    """
    data    = request.get_json(silent=True) or {}
    keyword = (data.get("keyword") or "").strip().upper()
    if not keyword:
        return jsonify({"error": "keyword is required"}), 400
    cfg = _load_transfer_config()
    kws = [k for k in cfg.get("internal_transfer_keywords", []) if k != keyword]
    cfg["internal_transfer_keywords"] = kws
    _save_transfer_config(cfg)
    log.info("Transfer keyword removed: %s", keyword)
    return jsonify({"status": "ok", "keywords": kws})


# ---------------------------------------------------------------------------
# Settings — categories
# ---------------------------------------------------------------------------

@dashboard_bp.route("/settings/categories/add", methods=["POST"])
@login_required
def settings_categories_add():
    """Add a category to an account type's list.

    Request:  { "account_type": "business", "category": "New Category" }
    Response: { "status": "ok", "categories": [...] }
    """
    data = request.get_json(silent=True) or {}
    acct_type = (data.get("account_type") or "").strip().lower()
    category  = (data.get("category") or "").strip()
    if not acct_type:
        return jsonify({"error": "account_type is required"}), 400
    if not category:
        return jsonify({"error": "category is required"}), 400

    settings = load_settings()
    cats = settings.setdefault("categories", {})
    acct_list = cats.setdefault(acct_type, [])
    if category not in acct_list:
        acct_list.append(category)
        save_settings(settings)
        log.info("Category added: %s → %s", acct_type, category)
    return jsonify({"status": "ok", "categories": acct_list})


@dashboard_bp.route("/settings/categories/remove", methods=["POST"])
@login_required
def settings_categories_remove():
    """Remove a category from an account type's list.

    Request:  { "account_type": "business", "category": "Old Category" }
    Response: { "status": "ok", "categories": [...] }
    """
    data = request.get_json(silent=True) or {}
    acct_type = (data.get("account_type") or "").strip().lower()
    category  = (data.get("category") or "").strip()
    if not acct_type or not category:
        return jsonify({"error": "account_type and category are required"}), 400

    settings = load_settings()
    cats = settings.get("categories", {})
    acct_list = [c for c in cats.get(acct_type, []) if c != category]
    cats[acct_type] = acct_list
    settings["categories"] = cats
    save_settings(settings)
    log.info("Category removed: %s → %s", acct_type, category)
    return jsonify({"status": "ok", "categories": acct_list})


@dashboard_bp.route("/settings/categories/reorder", methods=["POST"])
@login_required
def settings_categories_reorder():
    """Replace a full category list for one account type (for drag-reorder).

    Request:  { "account_type": "business", "categories": ["Revenue", "SaaS tools", ...] }
    Response: { "status": "ok" }
    """
    data = request.get_json(silent=True) or {}
    acct_type  = (data.get("account_type") or "").strip().lower()
    new_list   = data.get("categories", [])
    if not acct_type or not isinstance(new_list, list):
        return jsonify({"error": "account_type and categories list are required"}), 400

    settings = load_settings()
    settings.setdefault("categories", {})[acct_type] = new_list
    save_settings(settings)
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Settings — account types
# ---------------------------------------------------------------------------

@dashboard_bp.route("/settings/account-types/add", methods=["POST"])
@login_required
def settings_account_types_add():
    """Add a new account type.

    Request:  { "account_type": "freelance" }
    Response: { "status": "ok", "account_types": [...] }
    """
    data = request.get_json(silent=True) or {}
    acct_type = (data.get("account_type") or "").strip().lower()
    if not acct_type:
        return jsonify({"error": "account_type is required"}), 400

    settings = load_settings()
    types = settings.get("account_types", [])
    if acct_type not in types:
        types.append(acct_type)
        settings["account_types"] = types
        # Initialise with an empty category list if not already present
        settings.setdefault("categories", {})[acct_type] = \
            settings["categories"].get(acct_type, [])
        save_settings(settings)
        log.info("Account type added: %s", acct_type)
    return jsonify({"status": "ok", "account_types": types})


@dashboard_bp.route("/settings/account-types/remove", methods=["POST"])
@login_required
def settings_account_types_remove():
    """Remove an account type.

    Does NOT delete categories — they remain in settings.json so they can be
    restored if the account type is re-added later.

    Request:  { "account_type": "personal" }
    Response: { "status": "ok", "account_types": [...] }
    """
    data = request.get_json(silent=True) or {}
    acct_type = (data.get("account_type") or "").strip().lower()
    if not acct_type:
        return jsonify({"error": "account_type is required"}), 400

    settings = load_settings()
    types = [t for t in settings.get("account_types", []) if t != acct_type]
    if len(types) == 0:
        return jsonify({"error": "Cannot remove last account type"}), 400
    settings["account_types"] = types
    save_settings(settings)
    log.info("Account type removed: %s", acct_type)
    return jsonify({"status": "ok", "account_types": types})


# ---------------------------------------------------------------------------
# Pass-through detection
# ---------------------------------------------------------------------------

@dashboard_bp.route("/passthrough/scan", methods=["POST"])
@login_required
def passthrough_scan():
    """Three-pass scan: internal pairs, supplemental income, legacy pass-throughs.

    Request (optional): { "tolerance": 1.00, "window_days": 2 }
    Response: {
      "status": "ok",
      "internal_pairs":    [{"in": {...}, "out": {...}}, ...],
      "supplemental":      [{...}, ...],
      "passthrough_pairs": [{"in": {...}, "out": {...}}, ...],
    }
    """
    import csv as csv_mod
    from config import MASTER_TRANSACTIONS_CSV

    data        = request.get_json(silent=True) or {}
    tolerance   = float(data.get("tolerance",   PASSTHROUGH_TOLERANCE))
    window_days = int(data.get("window_days",   PASSTHROUGH_WINDOW_DAYS))

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return jsonify({"status": "ok", "internal_pairs": [], "supplemental": [], "passthrough_pairs": []})

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))

        result = scan_transactions(rows, tolerance=tolerance, window_days=window_days)

        def _fmt(r):
            try:
                amt = float(r.get("amount", 0))
            except ValueError:
                amt = 0.0
            return {
                "id":      r.get("transaction_id", ""),
                "date":    r.get("date", ""),
                "vendor":  r.get("vendor_name") or r.get("description", ""),
                "amount":  round(amt, 2),
                "account": r.get("account_type", ""),
                "card":    r.get("card_type", ""),
                "bank":    r.get("bank_name", ""),
            }

        return jsonify({
            "status": "ok",
            "internal_pairs":    [{"in": _fmt(p["in"]),  "out": _fmt(p["out"])}  for p in result["internal_pairs"]],
            "owner_draw_pairs":  [{"in": _fmt(p["in"]),  "out": _fmt(p["out"])}  for p in result["owner_draw_pairs"]],
            "supplemental":      [_fmt(r) for r in result["supplemental"]],
            "passthrough_pairs": [{"in": _fmt(p["in"]),  "out": _fmt(p["out"])}  for p in result["passthrough_pairs"]],
        })

    except Exception as e:
        log.error("Passthrough scan failed: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/passthrough/apply", methods=["POST"])
@login_required
def passthrough_apply():
    """Apply scan results: mark pass-through IDs and supplemental income IDs.

    Request: {
      "passthrough_ids":  ["TXN-...", ...],   # → Pass-through, exclude_from_pnl=True
      "supplemental_ids": ["TXN-...", ...],   # → Income/Supplemental, exclude_from_pnl=False
    }
    Response: { "status": "ok", "passthrough_updated": N, "supplemental_updated": N }
    """
    import csv as csv_mod, shutil
    from config import MASTER_TRANSACTIONS_CSV
    from csv_utils import TRANSACTION_HEADERS

    data             = request.get_json(silent=True) or {}
    passthrough_ids  = set(data.get("passthrough_ids",  []))
    supplemental_ids = set(data.get("supplemental_ids", []))
    owner_draw_ids   = set(data.get("owner_draw_ids",   []))

    if not passthrough_ids and not supplemental_ids and not owner_draw_ids:
        return jsonify({"error": "No transaction IDs provided"}), 400

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return jsonify({"error": "master_transactions.csv not found"}), 500

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))

        pt_updated  = 0
        sup_updated = 0
        od_updated  = 0

        for row in rows:
            tid = row.get("transaction_id", "")
            if tid in passthrough_ids:
                row["exclude_from_pnl"] = "True"
                row["category"]         = "Pass-through"
                row["categorized_by"]   = "manual"
                row["flagged"]          = "False"
                row["flag_reason"]      = ""
                pt_updated += 1
            elif tid in supplemental_ids:
                acct = row.get("account_type", "personal")
                row["category"]         = "Revenue" if acct == "business" else "Income"
                row["subcategory"]      = "Supplemental income"
                row["exclude_from_pnl"] = "False"
                row["categorized_by"]   = "manual"
                row["flagged"]          = "False"
                row["flag_reason"]      = ""
                sup_updated += 1
            elif tid in owner_draw_ids:
                acct = row.get("account_type", "personal")
                if acct == "business":
                    row["category"]    = "Owner's Draw"
                    row["subcategory"] = ""
                else:
                    row["category"]    = "Income"
                    row["subcategory"] = "Owner's Draw"
                row["vendor_name"]      = "Internal Transfer"
                row["exclude_from_pnl"] = "False"
                row["categorized_by"]   = "manual"
                row["flagged"]          = "False"
                row["flag_reason"]      = ""
                od_updated += 1

        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=TRANSACTION_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        shutil.move(str(tmp), str(path))

        log.info("Passthrough apply: %d pass-through, %d supplemental, %d owner's draw", pt_updated, sup_updated, od_updated)
        return jsonify({"status": "ok", "passthrough_updated": pt_updated, "supplemental_updated": sup_updated, "owner_draw_updated": od_updated})

    except Exception as e:
        log.error("Passthrough apply failed: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/passthrough/check", methods=["POST"])
@login_required
def passthrough_check():
    """Check whether a single transaction qualifies as part of a pass-through pair.

    Request:  { "transaction_id": "TXN-..." }
    Response: {
      "status":      "ok",
      "qualifies":   bool,
      "type":        "internal" | "owner_draw" | "passthrough" | null,
      "transaction": { id, date, vendor, amount, account, category },
      "match":       { id, date, vendor, amount, account, category } | null,
      "reasons":     ["...", ...],
    }
    """
    import csv as csv_mod
    from datetime import datetime as dt
    from config import MASTER_TRANSACTIONS_CSV, PASSTHROUGH_TOLERANCE, PASSTHROUGH_WINDOW_DAYS
    from dashboard.aggregator import _load_transfer_keywords

    data = request.get_json(silent=True) or {}
    tid  = data.get("transaction_id", "").strip()
    if not tid:
        return jsonify({"error": "transaction_id required"}), 400

    tolerance   = PASSTHROUGH_TOLERANCE
    window_days = PASSTHROUGH_WINDOW_DAYS

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return jsonify({"error": "master_transactions.csv not found"}), 500

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))

        # Find the target transaction
        target = next((r for r in rows if r.get("transaction_id") == tid), None)
        if not target:
            return jsonify({"error": f"Transaction {tid} not found"}), 404

        keywords = _load_transfer_keywords()
        kw_lower = [k.lower() for k in keywords]

        def is_internal(row):
            desc = row.get("description", "").lower()
            return any(k in desc for k in kw_lower)

        def parse_date(row):
            try:
                return dt.strptime(row["date"], "%Y-%m-%d")
            except (ValueError, KeyError):
                return None

        def fmt(row):
            try:
                amt = float(row.get("amount", 0))
            except ValueError:
                amt = 0.0
            return {
                "id":       row.get("transaction_id", ""),
                "date":     row.get("date", ""),
                "vendor":   row.get("vendor_name") or row.get("description", ""),
                "amount":   round(amt, 2),
                "account":  row.get("account_type", ""),
                "category": row.get("category") or "—",
                "bank":     row.get("bank_name") or "—",
            }

        try:
            t_amt  = float(target.get("amount", 0))
        except ValueError:
            t_amt = 0.0
        t_date    = parse_date(target)
        t_internal = is_internal(target)

        # Collect ALL candidate matches within tolerance/window
        candidates = []

        for row in rows:
            if row.get("transaction_id") == tid:
                continue

            try:
                r_amt = float(row.get("amount", 0))
            except ValueError:
                continue

            # Must be roughly opposite direction
            if t_amt >= 0 and r_amt >= 0:
                continue
            if t_amt < 0 and r_amt < 0:
                continue

            r_date = parse_date(row)
            if r_date is None or t_date is None:
                continue

            amt_diff = abs(abs(t_amt) - abs(r_amt))
            day_diff = abs((t_date - r_date).days)

            if amt_diff <= tolerance and day_diff <= window_days:
                candidates.append((row, amt_diff, day_diff))

        # Sort by closeness (day_diff primary, amt_diff secondary)
        candidates.sort(key=lambda x: (x[2], x[1]))

        if not candidates:
            return jsonify({
                "status":      "ok",
                "qualifies":   False,
                "type":        None,
                "transaction": fmt(target),
                "candidates":  [],
                "reasons":     ["No transaction found within the matching window "
                                f"(±${tolerance:.2f}, ±{window_days} days)."],
            })

        t_acct     = target.get("account_type", "")

        def build_candidate(row, amt_diff, day_diff):
            m_internal = is_internal(row)
            m_acct     = row.get("account_type", "")
            r_amt_val  = float(row.get("amount", 0))

            reasons = []
            diff_str = f"${amt_diff:.2f} difference" if amt_diff > 0 else "exact amount match"
            day_str  = f"{day_diff} day{'s' if day_diff != 1 else ''} apart" if day_diff > 0 else "same day"
            reasons.append(f"Amount: ${abs(t_amt):,.2f} — {diff_str}, {day_str}.")

            if t_internal and m_internal:
                if t_acct != m_acct:
                    pair_type = "owner_draw"
                    reasons.append(
                        f"Both legs are internal transfers but cross accounts "
                        f"({t_acct} → {m_acct}), indicating an owner's draw."
                    )
                    reasons.append(
                        "Business leg → Owner's Draw (expense). "
                        "Personal leg → Income / Owner's Draw."
                    )
                else:
                    pair_type = "internal"
                    reasons.append(
                        f"Both legs carry an internal transfer keyword and are in the "
                        f"same account type ({t_acct}) — same-account internal transfer."
                    )
                    reasons.append("Both transactions will be excluded from P&L.")
            else:
                pair_type = "passthrough"
                in_amt  = abs(t_amt) if t_amt > 0 else abs(r_amt_val)
                out_amt = abs(r_amt_val) if t_amt > 0 else abs(t_amt)
                in_v    = fmt(target)["vendor"] if t_amt > 0 else fmt(row)["vendor"]
                out_v   = fmt(row)["vendor"] if t_amt > 0 else fmt(target)["vendor"]
                reasons.append(
                    f"Inbound ${in_amt:,.2f} from {in_v} matched by outbound "
                    f"${out_amt:,.2f} to {out_v} — money received and forwarded on."
                )
                reasons.append("Both transactions will be excluded from P&L.")

            return {
                **fmt(row),
                "type":    pair_type,
                "reasons": reasons,
            }

        built = [build_candidate(r, a, d) for r, a, d in candidates]

        return jsonify({
            "status":      "ok",
            "qualifies":   True,
            "type":        built[0]["type"],   # type of top candidate (for badge)
            "transaction": fmt(target),
            "candidates":  built,
            "reasons":     [],                 # per-candidate now; kept for compat
        })

    except Exception as e:
        log.error("Passthrough check failed: %s", e)
        return jsonify({"error": str(e)}), 500
