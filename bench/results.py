"""Append-only result files, tagged with engine, subset, timestamp and code version."""
import csv
import platform
import subprocess
from datetime import datetime, timezone
from functools import cache

from . import config


@cache
def commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=config.ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"],
                               cwd=config.ROOT, capture_output=True, text=True).stdout.strip()
        return out + ("-dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def append(name: str, row: dict) -> None:
    path = config.RESULTS / f"{name}.csv"
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "host": platform.node().split(".")[0], "commit": commit(), **row}
    if path.exists():
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            fields, old = list(reader.fieldnames or []), list(reader)
        missing = [k for k in row if k not in fields]
        if missing:  # new column: rewrite once with the wider header, old rows get blanks
            fields += missing
            with path.open("w", newline="") as f:
                w = csv.DictWriter(f, fieldnames=fields)
                w.writeheader()
                w.writerows(old)
    else:
        fields = list(row)
    with path.open("a", newline="") as f:
        csv.DictWriter(f, fieldnames=fields, restval="").writerow(row)
