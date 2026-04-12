#!/bin/bash
# run.sh — start the bookkeeping dashboard
# Usage: ./run.sh
# Stop:  Ctrl+C

cd "$(dirname "$0")"

source ~/miniforge3/etc/profile.d/conda.sh
conda activate bookkeeping-system-env

echo "Starting bookkeeping dashboard..."
python app.py
