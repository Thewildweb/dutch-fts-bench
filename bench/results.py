"""Append-only result files, tagged with engine, subset and timestamp."""
import csv
import platform
from datetime import datetime, timezone

from . import config


def append(name: str, row: dict) -> None:
    path = config.RESULTS / f"{name}.csv"
    row = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "host": platform.node().split(".")[0], **row}
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row))
        if new:
            w.writeheader()
        w.writerow(row)
