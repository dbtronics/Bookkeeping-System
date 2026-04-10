import json
import logging
from functools import wraps
from pathlib import Path

from flask import Blueprint, session, redirect, url_for, render_template, request, jsonify

from config import RULES_JSON, RULES_ARCHIVE_DIR
from dashboard.aggregator import get_overview, get_business, get_personal, get_flagged

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
    return render_template("business.html", **data)


@dashboard_bp.route("/personal")
@login_required
def dashboard_personal():
    month = request.args.get("month")
    data = get_personal(month_filter=month)
    return render_template("personal.html", **data)


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
    return render_template("rules.html", rules=rules, suggested=suggested)


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

        # Remove from suggestions
        if suggested_path.exists():
            with open(suggested_path, encoding="utf-8") as f:
                sug_data = json.load(f)
            sug_data["suggestions"] = [
                s for s in sug_data.get("suggestions", [])
                if s.get("description") != suggestion.get("description")
            ]
            with open(suggested_path, "w", encoding="utf-8") as f:
                json.dump(sug_data, f, indent=2)

        log.info("Approved suggested rule: %s", new_rule["id"])
        return jsonify({"status": "ok", "rule_id": new_id})

    except Exception as e:
        log.error("Failed to approve rule: %s", e)
        return jsonify({"error": str(e)}), 500


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
