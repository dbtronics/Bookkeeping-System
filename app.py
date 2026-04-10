import logging
from functools import wraps
from flask import Flask, request, session, redirect, url_for, render_template, jsonify
from config import DASHBOARD_PASSWORD, FLASK_SECRET_KEY, FLASK_PORT

# Logging
logging.basicConfig(
    filename="bookkeeping.log",
    level=logging.INFO,
    format="%(asctime)s [app] %(levelname)s %(message)s"
)

app = Flask(__name__)
app.secret_key = FLASK_SECRET_KEY

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["authenticated"] = True
            logging.info("Login successful")
            return redirect(url_for("dashboard.dashboard_overview"))
        error = "Incorrect password."
        logging.warning("Failed login attempt")
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    logging.info("User logged out")
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
app.register_blueprint(receipts_bp)
app.register_blueprint(transactions_bp)
app.register_blueprint(query_bp)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=FLASK_PORT, debug=False)
