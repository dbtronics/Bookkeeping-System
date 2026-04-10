from flask import Blueprint, jsonify

transactions_bp = Blueprint("transactions", __name__)


@transactions_bp.route("/ingest/transaction", methods=["POST"])
def ingest_transaction():
    return jsonify({"status": "not implemented"}), 501
