from flask import Blueprint, jsonify

query_bp = Blueprint("query", __name__)


@query_bp.route("/query", methods=["POST"])
def nl_query():
    return jsonify({"status": "not implemented"}), 501
