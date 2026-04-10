#!/usr/bin/env python3
"""
raw_processor.py — Temporary standalone script
Reads raw bank CSVs → splits by month → writes to organized folder structure
→ appends new rows to master_transactions.csv with dedup + categorization.
Disable this once n8n + /ingest/transaction endpoint is live.
"""
import csv
import time
from datetime import datetime
from pathlib import Path

from logger import get_logger
from csv_utils import (
    TRANSACTION_HEADERS, ensure_csv, append_row,
    load_transaction_state, is_duplicate, register_transaction, read_csv,
)
from categorizer import load_rules, categorize, suggest_rules
from config import NEXTCLOUD_BASE, HAIKU_COST_PER_CALL
log = get_logger("raw_processor")
RAW_DIR        = NEXTCLOUD_BASE / "bank-transactions" / "raw"
MASTER_CSV     = NEXTCLOUD_BASE / "master" / "master_transactions.csv"

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

# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------

def parse_cibc(filepath):
    """No headers. Cols: date, description, debit, credit[, card]. Debit=out, credit=in."""
    rows = []
    with open(filepath, newline="", encoding="utf-8-sig") as f:
        for line in csv.reader(f):
            if len(line) < 4:
                continue
            date_str = line[0].strip()
            desc     = line[1].strip()
            debit    = line[2].strip()
            credit   = line[3].strip()
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
            desc1    = row.get("Description 1", "").strip()
            desc2    = row.get("Description 2", "").strip()
            cad      = row.get("CAD$", "").strip()
            if not date_str or not cad:
                continue
            try:
                dt = datetime.strptime(date_str, "%m/%d/%Y")
            except ValueError:
                continue
            desc = f"{desc1} {desc2}".strip() if desc2 else desc1
            rows.append({"date": dt.strftime("%Y-%m-%d"), "description": desc, "amount": float(cad)})
    return rows


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def group_by_month(rows):
    groups = {}
    for row in rows:
        dt = datetime.strptime(row["date"], "%Y-%m-%d")
        groups.setdefault((dt.year, dt.month), []).append(row)
    return groups


def write_organized_csv(rows, bank, account_type, year, month, source_stem):
    """Write normalized monthly CSV to structured folder. Returns dest path."""
    card_tag = source_stem.split("-")[-1]
    dest_dir = NEXTCLOUD_BASE / "bank-transactions" / account_type / str(year) / MONTHS[month]
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename = f"{bank.lower()}_{account_type}_{card_tag}_{MONTHS[month][:3]}{year}.csv"
    dest = dest_dir / filename
    with open(dest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "description", "amount"])
        for r in rows:
            w.writerow([r["date"], r["description"], r["amount"]])
    log.info("Wrote organised CSV → %s", dest.relative_to(NEXTCLOUD_BASE))
    return dest


def append_to_master(rows, bank, account_type, card_type, source_file,
                     existing_keys, id_counter, rules):
    """Categorize and append only new rows to master CSV.

    Returns (added, dupes, flagged, api_calls).
    """
    ensure_csv(MASTER_CSV, TRANSACTION_HEADERS)
    today                   = datetime.now().strftime("%Y-%m-%d")
    added = dupes = flagged = api_calls = 0

    for row in rows:
        if is_duplicate(row["date"], row["description"], row["amount"],
                        bank, account_type, card_type, existing_keys):
            dupes += 1
            log.debug("DUPE  %s  %s  $%.2f", row["date"], row["description"][:40], row["amount"])
            continue

        cat = categorize(
            {"description": row["description"], "account_type": account_type,
             "amount": row["amount"], "bank_name": bank},
            rules,
        )

        if cat["categorized_by"] == "ai":
            api_calls += 1

        txn_id = register_transaction(
            row["date"], row["description"], row["amount"],
            bank, account_type, card_type, existing_keys, id_counter,
        )

        append_row(MASTER_CSV, TRANSACTION_HEADERS, {
            "transaction_id":   txn_id,
            "source_file":      str(source_file),
            "import_date":      today,
            "date":             row["date"],
            "description":      row["description"],
            "vendor_name":      cat["vendor_name"],
            "amount":           f"{row['amount']:.2f}",
            "bank_name":        bank,
            "account_type":     account_type,
            "card_type":        card_type,
            "category":         cat["category"],
            "subcategory":      cat["subcategory"],
            "categorized_by":   cat["categorized_by"],
            "confidence":       cat["confidence"] if cat["confidence"] is not None else "",
            "flagged":          cat["flagged"],
            "flag_reason":      cat["flag_reason"],
            "exclude_from_pnl": cat["exclude_from_pnl"],
            "notes":            cat["notes"],
        })

        added += 1
        if cat["flagged"]:
            flagged += 1
            log.warning("FLAGGED  %s  %s  → %s (confidence=%.2f)",
                        row["date"], row["description"][:40],
                        cat["category"], cat["confidence"] or 0)

    return added, dupes, flagged, api_calls


# ---------------------------------------------------------------------------
# Per-file processor
# ---------------------------------------------------------------------------

def process_file(filepath, existing_keys, id_counter, rules):
    stem = filepath.stem
    if stem not in FILE_CONFIGS:
        log.warning("Skipping unknown file: %s (not in FILE_CONFIGS)", filepath.name)
        return 0, 0, 0, 0  # added, dupes, flagged, api_calls

    bank, account_type, card_type, parser = FILE_CONFIGS[stem]
    log.info("── Processing %s  (%s, %s, %s)", filepath.name, bank, account_type, card_type)

    try:
        rows = parse_cibc(filepath) if parser == "cibc" else parse_rbc(filepath)
    except Exception as e:
        log.error("Parse error in %s: %s", filepath.name, e)
        return 0, 0, 0, 0

    if not rows:
        log.warning("No rows parsed from %s", filepath.name)
        return 0, 0, 0, 0

    log.info("Parsed %d rows from %s", len(rows), filepath.name)

    file_added = file_dupes = file_flagged = file_api = 0

    for (year, month), month_rows in sorted(group_by_month(rows).items()):
        dest = write_organized_csv(month_rows, bank, account_type, year, month, stem)
        added, dupes, flagged, api_calls = append_to_master(
            month_rows, bank, account_type, card_type, dest,
            existing_keys, id_counter, rules,
        )
        log.info("  [%s %d] %d rows → +%d new, %d dupes, %d flagged, %d AI calls",
                 MONTHS[month], year, len(month_rows), added, dupes, flagged, api_calls)

        file_added   += added
        file_dupes   += dupes
        file_flagged += flagged
        file_api     += api_calls

    return file_added, file_dupes, file_flagged, file_api


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    start_time = time.time()
    log.info("=" * 60)
    log.info("raw_processor started")
    log.info("Scanning: %s", RAW_DIR)

    files = sorted(RAW_DIR.glob("*.csv"))
    if not files:
        log.warning("No CSV files found in raw/ — nothing to do")
        return

    log.info("Found %d file(s): %s", len(files), ", ".join(f.name for f in files))

    rules = load_rules()
    existing_keys, id_counter = load_transaction_state(MASTER_CSV)
    log.info("Master CSV has %d existing transactions", len(existing_keys))

    total_added = total_dupes = total_flagged = total_api = 0

    for f in files:
        added, dupes, flagged, api_calls = process_file(
            f, existing_keys, id_counter, rules
        )
        total_added   += added
        total_dupes   += dupes
        total_flagged += flagged
        total_api     += api_calls

    # Rule suggestions
    all_rows = read_csv(MASTER_CSV)
    ai_rows  = [r for r in all_rows if r.get("categorized_by") == "ai"]
    if ai_rows:
        suggest_rules(ai_rows)

    elapsed      = time.time() - start_time
    est_cost_cad = total_api * HAIKU_COST_PER_CALL * 1.38  # rough USD→CAD

    log.info("=" * 60)
    log.info("raw_processor finished in %.1fs", elapsed)
    log.info("Summary: %d files | +%d new | %d dupes | %d flagged | %d AI calls (~$%.4f CAD)",
             len(files), total_added, total_dupes, total_flagged, total_api, est_cost_cad)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
