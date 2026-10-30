"""Read the parquet dump. The file is read-only; every engine loads the same rows."""
import hashlib
from collections.abc import Iterator

import duckdb

from . import config

COLUMNS = ["id", "url", "domain", "title", "lang", "jobText", "textLength",
           "wordCount", "city", "firstSeen", "lastSeen"]


def verify() -> None:
    h = hashlib.sha256()
    with open(config.DATASET_PATH, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    if config.DATASET_SHA256 and h.hexdigest() != config.DATASET_SHA256:
        raise SystemExit(f"dataset checksum mismatch: {h.hexdigest()}")


def batches(subset: str, size: int = 5_000) -> Iterator[list[dict]]:
    limit = config.SUBSETS[subset]
    sql = f"SELECT {', '.join(COLUMNS)} FROM read_parquet(?) ORDER BY md5(id)"
    if limit:
        sql += f" LIMIT {limit}"
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    reader = con.sql(sql, params=[str(config.DATASET_PATH)]).to_arrow_reader(size)
    for batch in reader:
        yield batch.to_pylist()


def analyzer(lang: str) -> str:
    """Per-document analyzer choice, identical in every engine."""
    return {"nl": "dutch", "en": "english"}.get(lang, "simple")
