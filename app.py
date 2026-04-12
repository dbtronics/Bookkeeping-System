import time
from functools import wraps
from flask import Flask, request, session, redirect, url_for, render_template, jsonify
from config import DASHBOARD_PASSWORD, FLASK_SECRET_KEY, FLASK_PORT, AUTO_LOGIN
from logger import get_logger

log = get_logger("app")

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

@app.before_request
def _log_request_start():
    request._start_time = time.time()

@app.after_request
def _log_request_end(response):
    duration_ms = int((time.time() - getattr(request, "_start_time", time.time())) * 1000)
    # Skip static files — not useful in the log
    if not request.path.startswith("/static"):
        log.info("%s %s → %d  (%dms)", request.method, request.path, response.status_code, duration_ms)
    return response

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not AUTO_LOGIN and not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if AUTO_LOGIN:
        session["authenticated"] = True
        return redirect(url_for("dashboard.dashboard_overview"))
    error = None
    if request.method == "POST":
        received = request.form.get("password", "")
        log.info(
            "Login attempt from %s | received=%r (len=%d) | expected len=%d | match=%s",
            request.remote_addr, received, len(received),
            len(DASHBOARD_PASSWORD or ""), received == DASHBOARD_PASSWORD
        )
        if received == DASHBOARD_PASSWORD:
            session["authenticated"] = True
            log.info("Login successful from %s", request.remote_addr)
            return redirect(url_for("dashboard.dashboard_overview"))
        error = "Incorrect password. Please try again."
        log.warning("Failed login attempt from %s", request.remote_addr)
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    log.info("User logged out from %s", request.remote_addr)
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


# ---------------------------------------------------------------------------
# Register blueprints (stubbed — filled in later phases)
# ---------------------------------------------------------------------------

from dashboard.routes import dashboard_bp
from ingest.receipts import receipts_bp
from ingest.transactions import transactions_bp
from query.nl import query_bp

app.register_blueprint(dashboard_bp)

# Inject account_types + categories into every template so base.html
# can render the nav dynamically and modals can show the right options.
@app.context_processor
def inject_settings():
    from settings_utils import get_account_types, get_categories
    return {
        "nav_account_types": get_account_types(),
        "all_categories":    get_categories(),
    }
app.register_blueprint(receipts_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(query_bp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("Bookkeeping dashboard starting on 0.0.0.0:%d", FLASK_PORT)
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
