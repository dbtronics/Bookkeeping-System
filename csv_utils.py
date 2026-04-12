"""
csv_utils.py — The single place where all CSV reading and writing happens.

Every other module (ingest, dashboard, categorizer) calls these functions
instead of touching CSV files directly. This keeps the file-handling logic
in one place so bugs only need to be fixed once.

WHY THIS EXISTS:
  - CSV files are our database. If two parts of the app write to the same
    file differently, data gets corrupted or duplicated. This module
    enforces one consistent way to read and write.
  - It handles edge cases so callers don't have to think about them:
    file doesn't exist yet, encoding issues, missing columns, etc.

WHAT IT COVERS (transactions only — receipts are out of scope for now):
  - Creating the CSV with the right headers on first run
  - Reading all rows into memory as a list of dicts
  - Appending a single new row
  - Detecting duplicate transactions before writing
  - Generating unique transaction IDs (TXN-YYYYMMDD-NNNN format)
"""

import csv
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# The exact column order for master_transactions.csv.
# Every row written to that file must match this — no extra columns,
# no missing columns, same order every time.
TRANSACTION_HEADERS = [
    "transaction_id",    # Unique ID we generate: TXN-YYYYMMDD-NNNN
    "source_file",       # Which organized CSV file this row came from
    "import_date",       # When this device processed the transaction (today's date)
    "date",              # The actual transaction date from the bank statement
    "description",       # Raw bank description — never modified
    "vendor_name",       # Cleaned vendor name (from Claude or rules)
    "amount",            # Negative = expense, positive = income (CAD)
    "bank_name",         # e.g. CIBC, RBC
    "account_type",      # personal or business
    "card_type",         # chequing, credit, savings, loc
    "card_alias",        # optional label for a specific card product (e.g. aeroplan, dividend)
    "category",          # Top-level category (must be from the valid list in config.py)
    "subcategory",       # Optional finer detail within the category
    "categorized_by",    # How it was categorized: rule | ai | manual
    "confidence",        # 0.0–1.0 score from Claude. Null if rule or manual.
    "flagged",           # True if this row needs human review
    "flag_reason",       # Why it was flagged (e.g. "low confidence", "unusual amount")
    "exclude_from_pnl",  # True for inter-account transfers, CC payments, owner draws
                         # These still appear in the ledger but are excluded from totals
    "notes",             # Free text — used for things like "annual billing"
]


def ensure_csv(path, headers):
    """Create the CSV file with the correct headers if it doesn't exist yet.

    Safe to call every time the app starts — it does nothing if the file
    already exists. This means you never have to manually create the file.

    Args:
        path:    Full path to the CSV file (string or Path)
        headers: List of column names to write as the header row
    """
    path = Path(path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", newline="") as f:
            csv.writer(f).writerow(headers)
        log.info(f"Created new CSV: {path.name}")


def read_csv(path):
    """Read the entire CSV and return all rows as a list of dicts.

    Each row becomes a dict where keys are the column headers.
    For example: {"date": "2026-01-15", "amount": "-97.50", ...}

    Returns an empty list (not an error) if the file doesn't exist yet —
    this is normal on first run before any transactions have been imported.

    Args:
        path: Full path to the CSV file

    Returns:
        List of dicts, one per row. Empty list if file is missing.
    """
    path = Path(path)
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def append_row(path, headers, row):
    """Append a single row to the CSV without rewriting the whole file.

    Opens the file in append mode so existing data is never touched.
    The row must be a dict with keys matching the headers list.

    Args:
        path:    Full path to the CSV file
        headers: Column names (must match what the file was created with)
        row:     Dict of {column_name: value} to write
    """
    with open(path, "a", newline="") as f:
        csv.DictWriter(f, fieldnames=headers).writerow(row)


def load_transaction_state(path):
    """Read the master CSV once and build two lookup structures for an ingest run.

    This is the key performance function. Instead of reading the CSV once
    per transaction to check for duplicates (which would be very slow for
    500+ rows), we read it once at the start of a batch, load everything
    into memory, and then check/update memory for every transaction.

    Returns two objects that get passed around during an ingest run:

    existing_keys:
        A set of tuples — one per transaction already in the CSV.
        Each tuple is: (date, description, amount, bank, account_type, card_type)
        Used by is_duplicate() to check if a transaction is already recorded.
        Why these 6 fields? Because the same amount on the same date can
        legitimately appear on different cards (e.g. $100 payment on both
        business CC and personal CC). We need all 6 to uniquely identify
        a transaction without having an explicit transaction ID from the bank.

    id_counter:
        A dict of {date_key: max_n} where date_key is YYYYMMDD format.
        Used by register_transaction() to generate the next TXN ID.
        Example: {"20260115": 3} means TXN-20260115-0003 already exists,
        so the next one for that date will be TXN-20260115-0004.

    Args:
        path: Full path to master_transactions.csv

    Returns:
        (existing_keys set, id_counter dict)
    """
    existing_keys = set()
    id_counter = {}

    for row in read_csv(path):
        # Build the dedup key for this row
        try:
            existing_keys.add((
                row["date"],
                row["description"],
                f"{float(row['amount']):.2f}",
                row["bank_name"],
                row["account_type"],
                row["card_type"],
                row.get("card_alias", ""),
            ))
        except (ValueError, KeyError):
            continue  # Skip malformed rows — don't crash the whole import

        # Track the highest TXN sequence number seen for each date
        parts = row.get("transaction_id", "").split("-")
        if len(parts) == 3:
            try:
                id_counter[parts[1]] = max(id_counter.get(parts[1], 0), int(parts[2]))
            except ValueError:
                pass

    return existing_keys, id_counter


def is_duplicate(date, description, amount, bank_name, account_type, card_type, existing_keys,
                 card_alias=""):
    """Check if a transaction is already recorded in the master CSV.

    Uses the in-memory existing_keys set built by load_transaction_state()
    rather than re-reading the file. Always call load_transaction_state()
    once before calling this in a loop.

    Returns True if the transaction already exists — caller should skip it.
    Returns False if it's new — caller should write it.
    """
    try:
        key = (date, description, f"{float(amount):.2f}", bank_name, account_type, card_type,
               card_alias or "")
        return key in existing_keys
    except (ValueError, KeyError):
        return False


def register_transaction(date, description, amount, bank_name, account_type, card_type,
                         existing_keys, id_counter, card_alias=""):
    """Generate a unique TXN ID and mark the transaction as seen.

    Call this only after is_duplicate() has returned False.
    Updates both existing_keys and id_counter in place so subsequent
    calls within the same batch stay consistent without re-reading the file.

    Returns the new transaction ID string, e.g. "TXN-20260115-0004"
    """
    date_key = date.replace("-", "")
    n = id_counter.get(date_key, 0) + 1
    id_counter[date_key] = n
    existing_keys.add((date, description, f"{float(amount):.2f}", bank_name, account_type,
                       card_type, card_alias or ""))
    return f"TXN-{date_key}-{n:04d}"


def migrate_add_column(path, column_name, after_column=None):
    """Add a missing column to an existing CSV file in place.

    Safe to call even if the column already exists — does nothing in that case.
    New column is filled with empty strings for all existing rows.

    Args:
        path:         Full path to the CSV file
        column_name:  Name of the column to add
        after_column: Insert after this column; if None, appends at end
    """
    import shutil, tempfile
    path = Path(path)
    if not path.exists():
        return

    rows = list(read_csv(path))
    if not rows:
        return

    existing_headers = list(rows[0].keys())
    if column_name in existing_headers:
        return  # Already present — nothing to do

    # Determine insert position
    if after_column and after_column in existing_headers:
        idx = existing_headers.index(after_column) + 1
        new_headers = existing_headers[:idx] + [column_name] + existing_headers[idx:]
    else:
        new_headers = existing_headers + [column_name]

    tmp = path.with_suffix(".tmp.csv")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=new_headers)
            writer.writeheader()
            for row in rows:
                row.setdefault(column_name, "")
                writer.writerow(row)
        shutil.move(str(tmp), str(path))
        log.info(f"Migrated {path.name}: added column '{column_name}'")
    except Exception as e:
        log.error(f"Failed to migrate {path.name}: {e}")
        if tmp.exists():
            tmp.unlink()
