#!/bin/bash
# run.sh — ingest new transactions then start the bookkeeping dashboard
# Usage: ./run.sh
# Stop:  Ctrl+C

cd "$(dirname "$0")"

source ~/miniforge3/etc/profile.d/conda.sh
conda activate bookkeeping-system-env

echo "Checking for new transactions..."
python raw_processor.py

echo ""
echo "Starting bookkeeping dashboard..."
python app.py
