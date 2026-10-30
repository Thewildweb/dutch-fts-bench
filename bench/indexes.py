"""Build Postgres index arms and the Elasticsearch index, recording time and size."""
import json
import re
import time

import httpx
import psycopg

from . import config, dataset, results

PG_ARMS = ("trgm", "tsv", "fts", "bm25", "textsearch", "vchord")


def _arm_sql(arm: str, table: str) -> str:
    text = (config.ROOT / "sql" / "pg_indexes.sql").read_text()
    blocks = dict(re.findall(r"-- @arm (\w+)\n(.*?)(?=\n-- @arm |\Z)", text, re.S))
    return blocks[arm].replace("{table}", table)


def pg_sizes(conn, table: str) -> dict:
    row = conn.execute(
        "SELECT pg_table_size(%s), pg_indexes_size(%s)", [table, table]).fetchone()
    idx = conn.execute(
        "SELECT indexrelid::regclass::text, pg_relation_size(indexrelid) "
        "FROM pg_index WHERE indrelid = %s::regclass", [table]).fetchall()
    return {"table_bytes": row[0], "indexes_bytes": row[1], "per_index": dict(idx)}


def build_pg(arm: str, subset: str) -> dict:
    table = config.table_name(subset)
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        before = pg_sizes(conn, table)
        started = time.perf_counter()
        for stmt in filter(str.strip, _arm_sql(arm, table).split(";\n")):
            conn.execute(stmt)
        seconds = time.perf_counter() - started
        conn.execute(f"VACUUM ANALYZE {table}")
        after = pg_sizes(conn, table)
    added = {k: v for k, v in after["per_index"].items() if k not in before["per_index"]}
    row = {"engine": "postgres", "arm": arm, "subset": subset, "seconds": round(seconds, 1),
           "index_bytes": sum(added.values()),
           "table_growth_bytes": after["table_bytes"] - before["table_bytes"],
           "detail": json.dumps(added)}
    results.append("index", row)
    return row


def drop_pg(arm: str, subset: str) -> None:
    table = config.table_name(subset)
    names = {"trgm": [f"{table}_title_trgm", f"{table}_text_trgm"],
             "tsv": [], "fts": [f"{table}_tsv"], "bm25": [f"{table}_bm25"],
             "textsearch": [f"{table}_bm25_{l}" for l in ("nl", "en", "xx")],
             "vchord": [f"{table}_vc_{l}" for l in ("nl", "en", "xx")]}[arm]
    with psycopg.connect(config.PG_DSN, autocommit=True) as conn:
        for n in names:
            conn.execute(f"DROP INDEX IF EXISTS {n}")
        if arm == "tsv":
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS tsv")


# ----------------------------------------------------------- Elasticsearch

def _text_field(analyzer: str) -> dict:
    return {"type": "text", "analyzer": analyzer}


ES_MAPPING = {
    "settings": {"number_of_shards": 1, "number_of_replicas": 0, "refresh_interval": "-1"},
    "mappings": {"dynamic": "strict", "properties": {
        "id": {"type": "keyword"},
        "url": {"type": "keyword", "index": False},
        "domain": {"type": "keyword"},
        "lang": {"type": "keyword"},
        "city": {"type": "keyword"},
        "title": {"type": "keyword", "index": False},
        **{f"title_{l}": _text_field(a) for l, a in (("nl", "dutch"), ("en", "english"), ("xx", "standard"))},
        **{f"text_{l}": _text_field(a) for l, a in (("nl", "dutch"), ("en", "english"), ("xx", "standard"))},
        "text_length": {"type": "integer"},
        "word_count": {"type": "integer"},
        "first_seen": {"type": "date"},
        "last_seen": {"type": "date"},
    }},
}


def _es_doc(r: dict) -> dict:
    l = {"nl": "nl", "en": "en"}.get(r["lang"], "xx")
    return {"id": r["id"], "url": r["url"], "domain": r["domain"], "lang": r["lang"],
            "city": r["city"], "title": r["title"],
            f"title_{l}": r["title"], f"text_{l}": r["jobText"],
            "text_length": r["textLength"], "word_count": r["wordCount"],
            "first_seen": r["firstSeen"].isoformat(), "last_seen": r["lastSeen"].isoformat()}


def build_es(subset: str) -> dict:
    """Load and index. Idempotent: bulk `create` skips ids that already exist."""
    index = config.table_name(subset)
    es = httpx.Client(base_url=config.ES_URL, timeout=300)
    if es.head(f"/{index}").status_code == 404:
        es.put(f"/{index}", json=ES_MAPPING).raise_for_status()
    started = time.perf_counter()
    created = 0
    for batch in dataset.batches(subset, 5_000):
        lines = []
        for r in batch:
            lines.append(json.dumps({"create": {"_index": index, "_id": r["id"]}}))
            lines.append(json.dumps(_es_doc(r), ensure_ascii=False))
        resp = es.post("/_bulk", content="\n".join(lines) + "\n",
                       headers={"Content-Type": "application/x-ndjson"})
        resp.raise_for_status()
        for item in resp.json()["items"]:
            status = item["create"]["status"]
            if status == 201:
                created += 1
            elif status != 409:
                raise SystemExit(f"bulk error: {item}")
    load_seconds = time.perf_counter() - started
    es.put(f"/{index}/_settings", json={"index": {"refresh_interval": "1s"}}).raise_for_status()
    es.post(f"/{index}/_refresh").raise_for_status()
    es.post(f"/{index}/_forcemerge", params={"max_num_segments": 1}, timeout=3600).raise_for_status()
    seconds = time.perf_counter() - started
    stats = es.get(f"/{index}/_stats/store,docs").json()["indices"][index]["primaries"]
    row = {"engine": "elasticsearch", "arm": "default", "subset": subset,
           "seconds": round(seconds, 1), "index_bytes": stats["store"]["size_in_bytes"],
           "table_growth_bytes": 0,
           "detail": json.dumps({"created": created, "load_seconds": round(load_seconds, 1),
                                 "docs": stats["docs"]["count"]})}
    results.append("index", row)
    return row
