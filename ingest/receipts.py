from flask import Blueprint, jsonify

receipts_bp = Blueprint("receipts", __name__)


@receipts_bp.route("/ingest/receipt", methods=["POST"])
def ingest_receipt():
    return jsonify({"status": "not implemented"}), 501
