#!/usr/bin/env python3
"""
raw_processor.py — Temporary standalone script
Reads raw bank CSVs → splits by month → writes to organized folder structure
→ appends new rows to master_transactions.csv with dedup.
Disable this once n8n + /ingest/transaction endpoint is live.
"""
import os, csv, logging
from datetime import datetime
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

NEXTCLOUD_BASE = Path(os.environ["NEXTCLOUD_BASE"])
RAW_DIR = NEXTCLOUD_BASE / "bank-transactions" / "raw"
MASTER_CSV = NEXTCLOUD_BASE / "master" / "master_transactions.csv"

logging.basicConfig(
    filename=Path(__file__).parent / "bookkeeping.log",
    level=logging.INFO,
    format="%(asctime)s [raw_processor] %(levelname)s %(message)s"
)

MASTER_HEADERS = [
    "transaction_id", "source_file", "import_date", "date", "description",
    "vendor_name", "amount", "bank_name", "account_type", "card_type",
    "category", "subcategory", "categorized_by", "confidence",
    "flagged", "flag_reason", "exclude_from_pnl", "notes"
]

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


def load_master_state():
    """Load existing dedup keys and transaction ID counters from master CSV."""
    existing_keys, id_counter = set(), {}
    if not MASTER_CSV.exists():
        return existing_keys, id_counter
    with open(MASTER_CSV, newline="") as f:
        for row in csv.DictReader(f):
            try:
                existing_keys.add((row["date"], row["description"], f"{float(row['amount']):.2f}"))
            except (ValueError, KeyError):
                continue
            parts = row.get("transaction_id", "").split("-")
            if len(parts) == 3:
                id_counter[parts[1]] = max(id_counter.get(parts[1], 0), int(parts[2]))
    return existing_keys, id_counter


def ensure_master_csv():
    if not MASTER_CSV.exists():
        MASTER_CSV.parent.mkdir(parents=True, exist_ok=True)
        with open(MASTER_CSV, "w", newline="") as f:
            csv.writer(f).writerow(MASTER_HEADERS)


def append_to_master(rows, bank, account_type, card_type, source_file, existing_keys, id_counter):
    """Append only new (non-duplicate) rows to master CSV. Returns (added, dupes)."""
    ensure_master_csv()
    today = datetime.now().strftime("%Y-%m-%d")
    new_rows, added, dupes = [], 0, 0

    for row in rows:
        key = (row["date"], row["description"], f"{row['amount']:.2f}")
        if key in existing_keys:
            dupes += 1
            continue
        date_key = row["date"].replace("-", "")
        n = id_counter.get(date_key, 0) + 1
        id_counter[date_key] = n
        existing_keys.add(key)
        new_rows.append({
            "transaction_id":  f"TXN-{date_key}-{n:04d}",
            "source_file":     str(source_file),
            "import_date":     today,
            "date":            row["date"],
            "description":     row["description"],
            "vendor_name":     row["description"],
            "amount":          f"{row['amount']:.2f}",
            "bank_name":       bank,
            "account_type":    account_type,
            "card_type":       card_type,
            "category":        "",
            "subcategory":     "",
            "categorized_by":  "",
            "confidence":      "",
            "flagged":         False,
            "flag_reason":     "",
            "exclude_from_pnl": False,
            "notes":           ""
        })
        added += 1

    if new_rows:
        with open(MASTER_CSV, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=MASTER_HEADERS).writerows(new_rows)
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
    existing_keys, id_counter = load_master_state()
    for f in files:
        print(f"→ {f.name}")
        process_file(f, existing_keys, id_counter)
    print("\nDone. Check bookkeeping.log for details.")


if __name__ == "__main__":
    main()
