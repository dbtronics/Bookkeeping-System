import datetime
import json
import logging
import threading
from functools import wraps
from pathlib import Path

import anthropic
from flask import Blueprint, session, redirect, url_for, render_template, request, jsonify

from config import (
    RULES_JSON, RULES_ARCHIVE_DIR,
    ANTHROPIC_API_KEY, HAIKU_MODEL, SONNET_MODEL,
    NL_MODELS, NL_DEFAULT_MODEL, NL_MODEL_PRICING, USD_TO_CAD,
    BUSINESS_CATEGORIES, PERSONAL_CATEGORIES,
    PASSTHROUGH_TOLERANCE, PASSTHROUGH_WINDOW_DAYS,
)
from dashboard.aggregator import get_overview, get_business, get_personal, get_flagged, detect_passthrough_pairs

# Shared state for the recategorize background job (single-user app)
_recategorize_state = {}
_recategorize_lock  = threading.Lock()

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
    return render_template("business.html",
                           business_categories=BUSINESS_CATEGORIES,
                           personal_categories=PERSONAL_CATEGORIES,
                           **data)


@dashboard_bp.route("/personal")
@login_required
def dashboard_personal():
    month = request.args.get("month")
    data = get_personal(month_filter=month)
    return render_template("personal.html",
                           business_categories=BUSINESS_CATEGORIES,
                           personal_categories=PERSONAL_CATEGORIES,
                           **data)


@dashboard_bp.route("/flagged")
@login_required
def dashboard_flagged():
    month = request.args.get("month")
    data = get_flagged(month_filter=month)
    return render_template("flagged.html", **data)


@dashboard_bp.route("/rules")
@login_required
def dashboard_rules():
    rules = _load_rules()
    suggested = _load_suggested_rules()
    return render_template(
        "rules.html",
        rules=rules,
        suggested=suggested,
        business_categories=BUSINESS_CATEGORIES,
        personal_categories=PERSONAL_CATEGORIES,
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

    updated = 0
    rows = []

    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv_mod.DictReader(f))

    for row in rows:
        if row.get("categorized_by") == "manual":
            continue
        desc_match    = keyword and keyword in row.get("description", "").lower()
        account_match = not rule_account or rule_account == row.get("account_type", "")
        if desc_match and account_match:
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

    biz_cats = ", ".join(BUSINESS_CATEGORIES)
    per_cats = ", ".join(PERSONAL_CATEGORIES)

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
# Pass-through detection
# ---------------------------------------------------------------------------

@dashboard_bp.route("/passthrough/scan", methods=["POST"])
@login_required
def passthrough_scan():
    """Scan personal transactions for pass-through pairs.

    Request (optional):  { "tolerance": 1.00, "window_days": 5 }
    Response: { "status": "ok", "pairs": [...], "count": N }
    """
    import csv as csv_mod
    from config import MASTER_TRANSACTIONS_CSV

    data        = request.get_json(silent=True) or {}
    tolerance   = float(data.get("tolerance",   PASSTHROUGH_TOLERANCE))
    window_days = int(data.get("window_days",   PASSTHROUGH_WINDOW_DAYS))

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return jsonify({"status": "ok", "pairs": [], "count": 0})

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))

        pairs = detect_passthrough_pairs(rows, tolerance=tolerance, window_days=window_days)

        def _fmt(r):
            try:
                amt = float(r.get("amount", 0))
            except ValueError:
                amt = 0.0
            return {
                "id":       r.get("transaction_id", ""),
                "date":     r.get("date", ""),
                "vendor":   r.get("vendor_name") or r.get("description", ""),
                "amount":   round(amt, 2),
                "account":  r.get("account_type", ""),
                "card":     r.get("card_type", ""),
                "bank":     r.get("bank_name", ""),
            }

        result = [{"in": _fmt(p["in"]), "out": _fmt(p["out"])} for p in pairs]
        log.info("Passthrough scan: found %d pairs (tolerance=%.2f window=%dd)",
                 len(result), tolerance, window_days)
        return jsonify({"status": "ok", "pairs": result, "count": len(result)})

    except Exception as e:
        log.error("Passthrough scan failed: %s", e)
        return jsonify({"error": str(e)}), 500


@dashboard_bp.route("/passthrough/apply", methods=["POST"])
@login_required
def passthrough_apply():
    """Mark confirmed pass-through transaction IDs as excluded from P&L.

    Request:  { "transaction_ids": ["TXN-...", "TXN-..."] }
    Response: { "status": "ok", "updated": N }
    """
    import csv as csv_mod, shutil
    from config import MASTER_TRANSACTIONS_CSV
    from csv_utils import TRANSACTION_HEADERS

    data    = request.get_json(silent=True) or {}
    txn_ids = set(data.get("transaction_ids", []))

    if not txn_ids:
        return jsonify({"error": "No transaction IDs provided"}), 400

    path = Path(MASTER_TRANSACTIONS_CSV)
    if not path.exists():
        return jsonify({"error": "master_transactions.csv not found"}), 500

    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv_mod.DictReader(f))

        updated = 0
        for row in rows:
            if row.get("transaction_id") in txn_ids:
                row["exclude_from_pnl"] = "True"
                row["category"]         = "Pass-through"
                row["categorized_by"]   = "manual"
                row["flagged"]          = "False"
                row["flag_reason"]      = ""
                updated += 1

        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv_mod.DictWriter(f, fieldnames=TRANSACTION_HEADERS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        shutil.move(str(tmp), str(path))

        log.info("Passthrough apply: excluded %d transactions", updated)
        return jsonify({"status": "ok", "updated": updated})

    except Exception as e:
        log.error("Passthrough apply failed: %s", e)
        return jsonify({"error": str(e)}), 500
