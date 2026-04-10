"""
logger.py — shared logging configuration for all modules.

Sets up one logger that writes to:
  - bookkeeping.log  (persistent, structured, all levels)
  - stdout           (same output, so ./run.sh shows it in the terminal too)

Usage in any module:
    from logger import get_logger
    log = get_logger(__name__)
    log.info("...")
    log.warning("...")
    log.error("...")

Log format:
    2026-04-10 14:23:45  INFO     [raw_processor    ] Processing cibc-business-cc.csv
    2026-04-10 14:23:45  WARNING  [categorizer      ] Low confidence 0.45 — flagging
    2026-04-10 14:23:45  ERROR    [query.nl         ] Haiku API timeout
"""

import logging
import sys
from pathlib import Path

LOG_FILE = Path(__file__).parent / "bookkeeping.log"
_configured = False


def _setup():
    global _configured
    if _configured:
        return
    _configured = True

    fmt = logging.Formatter(
        fmt="%(asctime)s  %(levelname)-8s [%(name)-18s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # File handler — everything INFO and above goes to bookkeeping.log
    fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)

    # Stream handler — same output to stdout so ./run.sh shows it live
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(fmt)
    root.addHandler(sh)

    # Silence noisy third-party loggers
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger. Call _setup() on first use."""
    _setup()
    return logging.getLogger(name)
