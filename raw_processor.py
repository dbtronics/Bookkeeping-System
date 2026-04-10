#!/usr/bin/env python3
"""
raw_processor.py — Temporary standalone script
Reads raw bank CSVs → splits by month → writes to organized folder structure
→ appends new rows to master_transactions.csv with dedup.
Disable this once n8n + /ingest/transaction endpoint is live.

Uses csv_utils.py for all CSV operations so the logic stays consistent
with the rest of the app.
"""
import os, csv, logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

from csv_utils import (
    TRANSACTION_HEADERS, ensure_csv, append_row,
    load_transaction_state, is_duplicate, register_transaction
)

load_dotenv()

NEXTCLOUD_BASE = Path(os.environ["NEXTCLOUD_BASE"])
RAW_DIR = NEXTCLOUD_BASE / "bank-transactions" / "raw"
MASTER_CSV = NEXTCLOUD_BASE / "master" / "master_transactions.csv"

logging.basicConfig(
    filename=Path(__file__).parent / "bookkeeping.log",
    level=logging.INFO,
    format="%(asctime)s [raw_processor] %(levelname)s %(message)s"
)

MONTHS = {
    1: "january", 2: "february", 3: "march", 4: "april",
    5: "may", 6: "june", 7: "july", 8: "august",
    9: "september", 10: "october", 11: "november", 12: "december"
}

# filename stem → (bank, account_type, card_type, parser)
FILE_CONFIGS = {
    "cibc-business-cc":  ("CIBC", "business", "credit",   "cibc"),
    "cibc-business-dc":  ("CIBC", "business", "chequing", "cibc"),
    "cibc-personal-cc":  ("CIBC", "personal", "credit",   "cibc"),
    "cibc-personal-dc":  ("CIBC", "personal", "chequing", "cibc"),
    "cibc-personal-loc": ("CIBC", "personal", "credit",   "cibc"),
    "rbc-business-cc":   ("RBC",  "business", "credit",   "rbc"),
    "rbc-business-dc":   ("RBC",  "business", "chequing", "rbc"),
}


def parse_cibc(filepath):
    """No headers. Cols: date, description, debit, credit[, card]. Debit=out, credit=in."""
    rows = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for line in csv.reader(f):
            if len(line) < 4:
                continue
            date_str = line[0].strip()
            desc = line[1].strip()
            debit = line[2].strip()
            credit = line[3].strip()
            try:
                datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                continue
            amount = -float(debit) if debit else (float(credit) if credit else 0.0)
            rows.append({"date": date_str, "description": desc, "amount": amount})
    return rows


def parse_rbc(filepath):
    """Has headers. Date as M/D/YYYY. CAD$: negative=expense, positive=income."""
    rows = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            date_str = row.get("Transaction Date", "").strip()
            desc1 = row.get("Description 1", "").strip()
            desc2 = row.get("Description 2", "").strip()
            cad = row.get("CAD$", "").strip()
            if not date_str or not cad:
                continue
            try:
                dt = datetime.strptime(date_str, "%m/%d/%Y")
            except ValueError:
                continue
            desc = f"{desc1} {desc2}".strip() if desc2 else desc1
            rows.append({"date": dt.strftime("%Y-%m-%d"), "description": desc, "amount": float(cad)})
    return rows


def group_by_month(rows):
    groups = {}
    for row in rows:
        dt = datetime.strptime(row["date"], "%Y-%m-%d")
        groups.setdefault((dt.year, dt.month), []).append(row)
    return groups


def write_organized_csv(rows, bank, account_type, year, month, source_stem):
    """Write normalized monthly CSV to structured folder. Returns dest path."""
    card_tag = source_stem.split("-")[-1]  # cc / dc / loc
    dest_dir = NEXTCLOUD_BASE / "bank-transactions" / account_type / str(year) / MONTHS[month]
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{bank.lower()}_{account_type}_{card_tag}_{MONTHS[month][:3]}{year}.csv"
    dest = dest_dir / filename
    with open(dest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "description", "amount"])
        for r in rows:
            w.writerow([r["date"], r["description"], r["amount"]])
    return dest


def append_to_master(rows, bank, account_type, card_type, source_file, existing_keys, id_counter):
    """Append only new (non-duplicate) rows to master CSV. Returns (added, dupes)."""
    ensure_csv(MASTER_CSV, TRANSACTION_HEADERS)
    today = datetime.now().strftime("%Y-%m-%d")
    added, dupes = 0, 0

    for row in rows:
        if is_duplicate(row["date"], row["description"], row["amount"], bank, account_type, card_type, existing_keys):
            dupes += 1
            continue
        txn_id = register_transaction(
            row["date"], row["description"], row["amount"],
            bank, account_type, card_type, existing_keys, id_counter
        )
        append_row(MASTER_CSV, TRANSACTION_HEADERS, {
            "transaction_id":   txn_id,
            "source_file":      str(source_file),
            "import_date":      today,
            "date":             row["date"],
            "description":      row["description"],
            "vendor_name":      row["description"],
            "amount":           f"{row['amount']:.2f}",
            "bank_name":        bank,
            "account_type":     account_type,
            "card_type":        card_type,
            "category":         "",
            "subcategory":      "",
            "categorized_by":   "",
            "confidence":       "",
            "flagged":          False,
            "flag_reason":      "",
            "exclude_from_pnl": False,
            "notes":            ""
        })
        added += 1

    return added, dupes


def process_file(filepath, existing_keys, id_counter):
    stem = filepath.stem
    if stem not in FILE_CONFIGS:
        logging.warning(f"Skipping unknown file: {filepath.name}")
        print(f"  Skipping — not in FILE_CONFIGS")
        return

    bank, account_type, card_type, parser = FILE_CONFIGS[stem]
    logging.info(f"Processing {filepath.name} ({bank}, {account_type}, {card_type})")

    try:
        rows = parse_cibc(filepath) if parser == "cibc" else parse_rbc(filepath)
    except Exception as e:
        logging.error(f"Parse error {filepath.name}: {e}")
        print(f"  ERROR: {e}")
        return

    if not rows:
        logging.warning(f"No rows parsed from {filepath.name}")
        print(f"  No rows found.")
        return

    for (year, month), month_rows in sorted(group_by_month(rows).items()):
        dest = write_organized_csv(month_rows, bank, account_type, year, month, stem)
        added, dupes = append_to_master(
            month_rows, bank, account_type, card_type, dest, existing_keys, id_counter
        )
        msg = f"  [{MONTHS[month]} {year}] {len(month_rows)} rows → +{added} new, {dupes} dupes"
        print(msg)
        logging.info(msg)


def main():
    print(f"Scanning: {RAW_DIR}\n")
    files = sorted(RAW_DIR.glob("*.csv"))
    if not files:
        print("No CSV files found in raw/")
        return
    existing_keys, id_counter = load_transaction_state(MASTER_CSV)
    for f in files:
        print(f"→ {f.name}")
        process_file(f, existing_keys, id_counter)
    print("\nDone. Check bookkeeping.log for details.")


if __name__ == "__main__":
    main()
