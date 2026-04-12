#!/usr/bin/env python3
"""
raw_processor.py — Temporary standalone script
Reads raw bank CSVs → infers metadata from filename → shows rename confirmation
to the user (when filename doesn't conform to naming convention) → renames →
splits by month → writes to organized folder structure → appends new rows to
master_transactions.csv with dedup + categorization.

Naming convention:  {bank}-{account_type}-{card_type}[-{alias}]-{YYYYMMDD}-{YYYYMMDD}.csv
  Examples:
    cibc-personal-chequing-20260101-20260331.csv
    cibc-personal-credit-aeroplan-20260101-20260331.csv
    rbc-business-chequing-20260101-20260331.csv
"""
import csv
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from logger import get_logger
from csv_utils import (
    TRANSACTION_HEADERS, ensure_csv, append_row,
    load_transaction_state, is_duplicate, register_transaction,
    read_csv, migrate_add_column,
)
from categorizer import load_rules, categorize, suggest_rules
from config import NEXTCLOUD_BASE, HAIKU_COST_PER_CALL, USD_TO_CAD

log = get_logger("raw_processor")

RAW_DIR    = NEXTCLOUD_BASE / "bank-transactions" / "raw"
MASTER_CSV = NEXTCLOUD_BASE / "master" / "master_transactions.csv"

MONTHS = {
    1: "january", 2: "february", 3: "march", 4: "april",
    5: "may", 6: "june", 7: "july", 8: "august",
    9: "september", 10: "october", 11: "november", 12: "december"
}

KNOWN_BANKS    = ["cibc", "rbc", "td", "bmo", "scotiabank", "hsbc", "national", "desjardins"]
ACCOUNT_TYPES  = ["personal", "business"]
CARD_TYPES     = ["chequing", "credit", "savings", "loc"]

# Friendly display names for bank values stored in lowercase in filenames
BANK_DISPLAY = {
    "cibc":        "CIBC",
    "rbc":         "RBC",
    "td":          "TD",
    "bmo":         "BMO",
    "scotiabank":  "Scotia",
    "hsbc":        "HSBC",
    "national":    "National",
    "desjardins":  "Desjardins",
}

# Parser to use per bank: default cibc unless the bank has a dedicated one
BANK_PARSER = {
    "rbc": "rbc",
}


# ---------------------------------------------------------------------------
# Nomenclature detection
# ---------------------------------------------------------------------------

def _nomenclature_pattern():
    """Build the regex for the standard filename convention (compiled once)."""
    banks = "|".join(re.escape(b) for b in KNOWN_BANKS)
    accts = "|".join(re.escape(a) for a in ACCOUNT_TYPES)
    cards = "|".join(re.escape(c) for c in CARD_TYPES)
    # Alias: optional, must start with a letter (to not collide with date blocks)
    return re.compile(
        rf'^({banks})-({accts})-({cards})(?:-([a-z][a-z0-9-]*))?-(\d{{8}})-(\d{{8}})$'
    )

_NOMENCLATURE_RE = _nomenclature_pattern()


def conforms_to_nomenclature(stem):
    """Check if a filename stem already follows the standard naming convention.

    Returns (True, metadata_dict) or (False, {}).
    metadata_dict keys: bank, account_type, card_type, card_alias, date_from, date_to
    """
    m = _NOMENCLATURE_RE.match(stem.lower())
    if not m:
        return False, {}
    bank_s, acct_s, card_s, alias_s, d1, d2 = m.groups()
    return True, {
        "bank":         BANK_DISPLAY.get(bank_s, bank_s.upper()),
        "account_type": acct_s,
        "card_type":    card_s,
        "card_alias":   alias_s or "",
        "date_from":    f"{d1[:4]}-{d1[4:6]}-{d1[6:]}",
        "date_to":      f"{d2[:4]}-{d2[4:6]}-{d2[6:]}",
    }


def infer_from_filename(stem):
    """Try to extract bank, account_type, card_type, card_alias from filename keywords.

    Returns (bank, account_type, card_type, card_alias) — any may be None/"" if
    it could not be determined.
    """
    s = stem.lower()

    bank = None
    for b in KNOWN_BANKS:
        if b in s:
            bank = BANK_DISPLAY.get(b, b.upper())
            break

    account_type = None
    if "personal" in s:
        account_type = "personal"
    elif "business" in s or "corp" in s:
        account_type = "business"

    # Card type — check LOC first (most specific), then credit, then chequing, then savings
    card_type = None
    if re.search(r'(^|[-_])loc($|[-_])', s) or "lineofcredit" in s:
        card_type = "loc"
    elif re.search(r'(^|[-_])cc($|[-_])', s) or "credit" in s:
        card_type = "credit"
    elif (re.search(r'(^|[-_])dc($|[-_])', s)
          or "chequing" in s or "checking" in s or "cheque" in s or "debit" in s):
        card_type = "chequing"
    elif "savings" in s or "saving" in s:
        card_type = "savings"

    # Card alias: parts that aren't a known keyword or a date block
    card_alias = _extract_alias(s, bank, account_type, card_type)

    return bank, account_type, card_type, card_alias


def _extract_alias(stem_lower, bank, account_type, card_type):
    """Extract any extra label parts from the stem that aren't known keywords or dates."""
    known = set(KNOWN_BANKS) | set(ACCOUNT_TYPES) | set(CARD_TYPES)
    known |= {"cc", "dc", "debit", "chequing", "checking", "cheque",
               "credit", "savings", "saving", "loc", "corp",
               "lineofcredit", "line-of-credit"}
    if bank:
        known.add(bank.lower())

    parts = re.split(r'[-_]', stem_lower)
    alias_parts = []
    for p in parts:
        if not p:
            continue
        if p in known:
            continue
        if re.match(r'^\d+$', p):   # pure digits → date block
            continue
        alias_parts.append(p)
    return "-".join(alias_parts)


# ---------------------------------------------------------------------------
# Filename builder
# ---------------------------------------------------------------------------

def build_new_stem(bank, account_type, card_type, card_alias, start_date, end_date):
    """Build the standard filename stem (without .csv extension).

    Format: {bank}-{account_type}-{card_type}[-{alias}]-{YYYYMMDD}-{YYYYMMDD}
    """
    parts = [bank.lower(), account_type, card_type]
    if card_alias:
        # Sanitize alias: lowercase, only alphanumeric and hyphens
        clean = re.sub(r'[^a-z0-9-]', '', card_alias.lower().replace(" ", "-"))
        if clean:
            parts.append(clean)
    if start_date and end_date:
        parts.append(start_date.replace("-", ""))
        parts.append(end_date.replace("-", ""))
    return "-".join(parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_preview_lines(filepath, n=5):
    """Return the first n non-empty raw lines from the file as strings."""
    lines = []
    try:
        with open(filepath, encoding="utf-8-sig", errors="replace") as f:
            for line in f:
                stripped = line.rstrip("\n\r")
                if stripped.strip():
                    lines.append(stripped)
                if len(lines) >= n:
                    break
    except Exception as e:
        log.warning("Could not read preview from %s: %s", filepath.name, e)
    return lines


def get_date_range(rows):
    """Return (start_date_str, end_date_str) from a list of parsed rows. Either may be None."""
    dates = [r["date"] for r in rows if r.get("date")]
    if not dates:
        return None, None
    return min(dates), max(dates)


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


def _parse(filepath, bank):
    """Parse a file using the appropriate parser for the given bank."""
    parser_name = BANK_PARSER.get(bank.lower() if bank else "", "cibc")
    return parse_rbc(filepath) if parser_name == "rbc" else parse_cibc(filepath)


def group_by_month(rows):
    groups = {}
    for row in rows:
        dt = datetime.strptime(row["date"], "%Y-%m-%d")
        groups.setdefault((dt.year, dt.month), []).append(row)
    return groups


# ---------------------------------------------------------------------------
# Organized CSV + master CSV writers
# ---------------------------------------------------------------------------

def write_organized_csv(rows, bank, account_type, card_type, card_alias, year, month):
    """Write normalized monthly CSV to structured folder. Returns dest path."""
    alias_tag = f"_{card_alias}" if card_alias else ""
    dest_dir  = NEXTCLOUD_BASE / "bank-transactions" / account_type / str(year) / MONTHS[month]
    dest_dir.mkdir(parents=True, exist_ok=True)
    filename  = f"{bank.lower()}_{account_type}_{card_type}{alias_tag}_{MONTHS[month][:3]}{year}.csv"
    dest      = dest_dir / filename
    with open(dest, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "description", "amount"])
        for r in rows:
            w.writerow([r["date"], r["description"], r["amount"]])
    log.info("Wrote organised CSV → %s", dest.relative_to(NEXTCLOUD_BASE))
    return dest


def append_to_master(rows, bank, account_type, card_type, card_alias, source_file,
                     existing_keys, id_counter, rules):
    """Categorize and append only new rows to master CSV.

    Returns (added, dupes, flagged, rule_matched, ai_categorized).
    """
    ensure_csv(MASTER_CSV, TRANSACTION_HEADERS)
    today = datetime.now().strftime("%Y-%m-%d")
    added = dupes = flagged = rule_matched = ai_categorized = 0

    for row in rows:
        if is_duplicate(row["date"], row["description"], row["amount"],
                        bank, account_type, card_type, existing_keys, card_alias):
            dupes += 1
            log.debug("DUPE  %s  %s  $%.2f", row["date"], row["description"][:40], row["amount"])
            continue

        cat = categorize(
            {"description": row["description"], "account_type": account_type,
             "amount": row["amount"], "bank_name": bank, "card_type": card_type},
            rules,
        )

        if cat["categorized_by"] == "ai":
            ai_categorized += 1
        elif cat["categorized_by"] == "rule":
            rule_matched += 1

        txn_id = register_transaction(
            row["date"], row["description"], row["amount"],
            bank, account_type, card_type, existing_keys, id_counter, card_alias,
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
            "card_alias":       card_alias or "",
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

    return added, dupes, flagged, rule_matched, ai_categorized


# ---------------------------------------------------------------------------
# Per-file processor
# ---------------------------------------------------------------------------

def process_file(filepath, bank, account_type, card_type, card_alias,
                 existing_keys, id_counter, rules):
    """Process a single raw CSV file with pre-determined metadata.

    Returns (added, dupes, flagged, rule_matched, ai_categorized).
    """
    log.info("── Processing %s  (%s, %s, %s, alias=%r)",
             filepath.name, bank, account_type, card_type, card_alias)

    try:
        rows = _parse(filepath, bank)
    except Exception as e:
        log.error("Parse error in %s: %s", filepath.name, e)
        return 0, 0, 0, 0, 0

    if not rows:
        log.warning("No rows parsed from %s", filepath.name)
        return 0, 0, 0, 0, 0

    log.info("Parsed %d rows from %s", len(rows), filepath.name)

    file_added = file_dupes = file_flagged = file_rule = file_ai = 0

    for (year, month), month_rows in sorted(group_by_month(rows).items()):
        dest = write_organized_csv(month_rows, bank, account_type, card_type, card_alias, year, month)
        added, dupes, flagged, rule_matched, ai_categorized = append_to_master(
            month_rows, bank, account_type, card_type, card_alias, dest,
            existing_keys, id_counter, rules,
        )
        log.info("  [%s %d] %d rows → +%d new, %d dupes, %d flagged, %d rules, %d AI",
                 MONTHS[month], year, len(month_rows), added, dupes, flagged, rule_matched, ai_categorized)

        file_added   += added
        file_dupes   += dupes
        file_flagged += flagged
        file_rule    += rule_matched
        file_ai      += ai_categorized

    return file_added, file_dupes, file_flagged, file_rule, file_ai


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run_with_progress(state):
    """Run the full processing pipeline, updating `state` dict for UI polling.

    state keys written:
      phase          : "scanning" | "processing" | "waiting_input" | "done"
      files_found    : int
      files_done     : int
      current_file   : str
      files          : list of per-file result dicts
      totals         : {added, dupes, flagged, rule_matched, ai_categorized, api_cost_cad}
      waiting_for    : dict present only when phase="waiting_input" (filename, preview,
                       detected fields, need list)
      answer         : dict written by /process/answer when user submits
      error          : str | None
      done           : bool
      running        : bool
      _input_event   : threading.Event (not JSON-safe — stripped in /process/status)
    """
    input_event = threading.Event()
    state["_input_event"] = input_event

    try:
        state.update({
            "phase": "scanning", "files_found": 0, "files_done": 0,
            "current_file": "", "files": [],
            "totals": {"added": 0, "dupes": 0, "flagged": 0,
                       "rule_matched": 0, "ai_categorized": 0, "api_cost_cad": 0.0},
            "error": None,
        })

        # Migrate master CSV to add card_alias column if missing
        if MASTER_CSV.exists():
            migrate_add_column(MASTER_CSV, "card_alias", after_column="card_type")

        files = sorted(RAW_DIR.glob("*.csv"))
        state["files_found"] = len(files)

        if not files:
            state.update({"phase": "done", "done": True, "running": False})
            return

        rules = load_rules()
        existing_keys, id_counter = load_transaction_state(MASTER_CSV)
        state["phase"] = "processing"

        for f in files:
            state["current_file"] = f.name
            original_name         = f.name

            # ── Step 1: Check if filename already conforms ──
            conforms, meta = conforms_to_nomenclature(f.stem)

            if conforms:
                # All metadata comes from the filename — no user prompt needed
                bank         = meta["bank"]
                account_type = meta["account_type"]
                card_type    = meta["card_type"]
                card_alias   = meta["card_alias"]
                log.info("Conforms to naming convention: %s", f.name)

            else:
                # ── Step 2: Infer what we can, then ask user to confirm/fill ──
                inf_bank, inf_acct, inf_card, inf_alias = infer_from_filename(f.stem)

                # Try to get date range for context (best-effort parse)
                try:
                    rows_peek = _parse(f, inf_bank or "cibc")
                except Exception:
                    rows_peek = []
                start_date, end_date = get_date_range(rows_peek)

                preview = get_preview_lines(f, 5)

                # Pause thread and wait for user input
                input_event.clear()
                state["phase"]       = "waiting_input"
                state["waiting_for"] = {
                    "filename":  f.name,
                    "preview":   preview,
                    "detected": {
                        "bank":         inf_bank,
                        "account_type": inf_acct,
                        "card_type":    inf_card,
                        "card_alias":   inf_alias,
                        "date_from":    start_date,
                        "date_to":      end_date,
                    },
                }
                log.info("Waiting for user input on: %s", f.name)
                input_event.wait()   # Blocks until /process/answer fires the event

                # Read answer submitted by user
                ans          = state.get("answer", {})
                bank         = ans.get("bank")  or inf_bank  or "UNKNOWN"
                account_type = ans.get("account_type") or inf_acct  or "personal"
                card_type    = ans.get("card_type")    or inf_card  or "chequing"
                card_alias   = ans.get("card_alias",   inf_alias or "")

                state["phase"] = "processing"
                state.pop("waiting_for", None)
                state.pop("answer", None)

                # ── Step 3: Parse file for date range + renaming ──
                try:
                    rows_peek = _parse(f, bank)
                except Exception:
                    rows_peek = []
                start_date, end_date = get_date_range(rows_peek)

                # ── Step 4: Rename raw file to standard convention ──
                new_stem = build_new_stem(bank, account_type, card_type, card_alias,
                                          start_date, end_date)
                new_path = f.parent / (new_stem + ".csv")
                if new_path != f:
                    if new_path.exists():
                        log.warning("Rename target already exists — skipping rename: %s", new_path.name)
                    else:
                        try:
                            f.rename(new_path)
                            log.info("Renamed %s → %s", original_name, new_path.name)
                            f = new_path
                        except OSError as e:
                            log.warning("Could not rename %s: %s", original_name, e)

            state["current_file"] = f.name

            # ── Step 5: Process the file ──
            try:
                added, dupes, flagged, rule_matched, ai_categorized = process_file(
                    f, bank, account_type, card_type, card_alias,
                    existing_keys, id_counter, rules,
                )
                status = "done"
                note   = ""
            except Exception as e:
                log.error("Error processing %s: %s", f.name, e)
                added = dupes = flagged = rule_matched = ai_categorized = 0
                status = "error"
                note   = str(e)

            file_cost = round(ai_categorized * HAIKU_COST_PER_CALL * USD_TO_CAD, 4)
            state["files"].append({
                "name":           f.name,
                "original_name":  original_name,
                "renamed":        (f.name != original_name),
                "status":         status,
                "added":          added,
                "dupes":          dupes,
                "flagged":        flagged,
                "rule_matched":   rule_matched,
                "ai_categorized": ai_categorized,
                "api_cost_cad":   file_cost,
                "note":           note,
            })
            t = state["totals"]
            t["added"]          += added
            t["dupes"]          += dupes
            t["flagged"]        += flagged
            t["rule_matched"]   += rule_matched
            t["ai_categorized"] += ai_categorized
            t["api_cost_cad"]    = round(t["api_cost_cad"] + file_cost, 4)
            state["files_done"] += 1

        # Rule suggestions after full run
        try:
            all_rows = read_csv(MASTER_CSV)
            ai_rows  = [r for r in all_rows if r.get("categorized_by") == "ai"]
            if ai_rows:
                suggest_rules(ai_rows)
        except Exception as e:
            log.warning("suggest_rules failed: %s", e)

        state.update({"phase": "done", "done": True, "running": False, "current_file": ""})

    except Exception as e:
        log.error("run_with_progress failed: %s", e)
        state.update({"error": str(e), "done": True, "running": False, "phase": "done"})


def main():
    start_time = time.time()
    log.info("=" * 60)
    log.info("raw_processor started")
    log.info("Scanning: %s", RAW_DIR)

    state = {"running": True}
    run_with_progress(state)

    t            = state.get("totals", {})
    elapsed      = time.time() - start_time
    ai_calls     = t.get("ai_categorized", 0)
    est_cost_cad = ai_calls * HAIKU_COST_PER_CALL * USD_TO_CAD

    log.info("=" * 60)
    log.info("raw_processor finished in %.1fs", elapsed)
    log.info("Summary: %d files | +%d new | %d dupes | %d flagged | %d rules | %d AI (~$%.4f CAD)",
             state.get("files_found", 0), t.get("added", 0), t.get("dupes", 0),
             t.get("flagged", 0), t.get("rule_matched", 0), ai_calls, est_cost_cad)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
