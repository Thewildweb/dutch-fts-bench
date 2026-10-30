"""Load the parquet rows into Postgres. Idempotent: existing ids are skipped."""
import time

import psycopg

from . import config, dataset

COLS = ["id", "url", "domain", "title", "lang", "job_text", "text_length",
        "word_count", "city", "first_seen", "last_seen"]


def load(subset: str) -> dict:
    table = config.table_name(subset)
    ddl = (config.ROOT / "sql" / "pg_table.sql").read_text().replace("{table}", table)
    started = time.perf_counter()
    rows = 0
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        conn.execute(ddl)
        conn.execute("CREATE TEMP TABLE staging (LIKE %s)" % table)
        for batch in dataset.batches(subset, 20_000):
            with conn.transaction():
                conn.execute("TRUNCATE staging")
                with conn.cursor().copy(f"COPY staging ({', '.join(COLS)}) FROM STDIN") as cp:
                    for r in batch:
                        cp.write_row([r[c] for c in dataset.COLUMNS])
                cur = conn.execute(f"INSERT INTO {table} SELECT * FROM staging ON CONFLICT (id) DO NOTHING")
                rows += cur.rowcount
        conn.execute(f"VACUUM ANALYZE {table}")
    return {"table": table, "inserted": rows, "seconds": round(time.perf_counter() - started, 1)}
