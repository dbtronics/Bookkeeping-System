#!/bin/bash
# raw_processor.sh — ingest raw bank CSVs into master_transactions.csv
# Usage: ./raw_processor.sh
#
# Drop raw bank CSVs into:
#   Nextcloud/Bookkeeping-System/bank-transactions/raw/
# then run this script.
#
# What it does:
#   1. Splits each file by month → organised folder structure
#   2. Categorizes new rows (rules first, Claude Haiku fallback)
#   3. Appends only new rows to master_transactions.csv (dedup is automatic)
#   4. Writes rules_suggested.json for review in the dashboard
#
# Already-processed rows are skipped instantly — safe to run any time.

cd "$(dirname "$0")"

source ~/miniforge3/etc/profile.d/conda.sh
conda activate bookkeeping-system-env

echo "Running raw processor..."
python raw_processor.py
