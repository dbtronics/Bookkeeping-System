from flask import Blueprint, session, redirect, url_for
from functools import wraps

dashboard_bp = Blueprint("dashboard", __name__)


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@dashboard_bp.route("/")
@dashboard_bp.route("/dashboard")
@login_required
def dashboard_overview():
    return "Dashboard — coming in Phase 9"


@dashboard_bp.route("/business")
@login_required
def dashboard_business():
    return "Business — coming in Phase 9"


@dashboard_bp.route("/personal")
@login_required
def dashboard_personal():
    return "Personal — coming in Phase 9"


@dashboard_bp.route("/receipts")
@login_required
def dashboard_receipts():
    return "Receipts — coming in Phase 9"
