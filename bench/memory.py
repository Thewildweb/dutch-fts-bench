"""How much RAM does each search arm need? Runs on the benchmark host (needs Docker).

working_set: start the engine with nothing of its data cached (container stopped, files
  evicted from the page cache), run the query set, then record per relation (Postgres) or
  per Lucene file type (Elasticsearch) how much sits in shared_buffers and the OS page
  cache, plus the container's memory as the kernel counts it. Measured after the top-10
  queries and again after the facet counts, since facets touch other data.
squeeze: restart the engine under a container memory limit (Postgres shared_buffers = 1/4 of
  it, Elasticsearch heap = 1/2, the usual advice) and rerun latency, to find where p99 breaks.
heap: Elasticsearch only; vary the JVM heap with the default container limit.
cold: cold-start latency, every query once right after a cold start.

Rows go to results/memory.csv; latency rows from squeeze/heap go to results/latency.csv
with a label such as "mem=2g".
"""
import json
import os
import re
import subprocess
import time
from collections import defaultdict

import httpx
import psycopg

from . import config, results
from .run import latency

MB = 1024 * 1024
HELPER_IMAGE = "python:3.13-slim"


def service_of(engine: str) -> str:
    if engine.startswith("elasticsearch"):
        return "elasticsearch"
    return "pg-textsearch" if engine.startswith("pg-textsearch") else "postgres"


def _bytes(size: str) -> int:
    n, unit = re.fullmatch(r"(\d+)([mg])", size.lower()).groups()
    return int(n) * (MB if unit == "m" else 1024 * MB)


def compose(*args: str, env: dict | None = None) -> str:
    return subprocess.run(["docker", "compose", *args], cwd=config.ROOT, check=True,
                          capture_output=True, text=True,
                          env={**os.environ, **(env or {})}).stdout.strip()


def container_id(service: str) -> str:
    return compose("ps", "-q", "--no-trunc", service)


def volume_of(service: str) -> str:
    cid = compose("ps", "-aq", "--no-trunc", service)
    out = subprocess.run(["docker", "inspect", cid, "--format",
                          "{{range .Mounts}}{{if .Name}}{{.Name}}{{end}}{{end}}"],
                         check=True, capture_output=True, text=True).stdout.strip()
    return out


def pagecache(volume: str, mode: str) -> list[dict]:
    script = config.ROOT / "scripts" / "pagecache.py"
    out = subprocess.run(["docker", "run", "--rm", "-v", f"{volume}:/data:ro",
                          "-v", f"{script}:/p.py:ro", HELPER_IMAGE, "python", "/p.py", mode,
                          "/data"], check=True, capture_output=True, text=True).stdout
    return [json.loads(line) for line in out.splitlines()]


def wait_ready(service: str, timeout: float = 300) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if service == "elasticsearch":
                r = httpx.get(f"{config.ES_URL}/_cluster/health",
                              params={"wait_for_status": "yellow", "timeout": "5s"}, timeout=10)
                if r.status_code == 200:
                    return
            else:
                with psycopg.connect(config.PG_DSN, connect_timeout=3) as c:
                    c.execute("SELECT 1")
                    return
        except (httpx.HTTPError, psycopg.Error):
            pass
        time.sleep(2)
    raise RuntimeError(f"{service} not ready after {timeout}s")


def cold_start(service: str, env: dict | None = None) -> None:
    """Stop every engine, evict this one's files from the page cache, start it fresh."""
    compose("stop")
    pagecache(volume_of(service), "evict")
    compose("up", "-d", service, env=env)
    wait_ready(service)


def cgroup(service: str) -> dict:
    base = f"/sys/fs/cgroup/system.slice/docker-{container_id(service)}.scope"
    stat = dict(line.split() for line in open(f"{base}/memory.stat"))
    events = dict(line.split() for line in open(f"{base}/memory.events"))
    return {"cg_current_mb": round(int(open(f"{base}/memory.current").read()) / MB),
            "cg_anon_mb": round(int(stat["anon"]) / MB),
            "cg_file_mb": round(int(stat["file"]) / MB),
            "cg_shmem_mb": round(int(stat["shmem"]) / MB),
            "cg_oom_kills": int(events.get("oom_kill", 0))}


# ---- Postgres --------------------------------------------------------------------------------

PG_RELATIONS = """
SELECT pg_relation_filenode(c.oid)::text, c.relkind,
       coalesce(t.relname || ' (toast)', ti.relname || ' (toast index)', c.relname),
       pg_relation_size(c.oid)
FROM pg_class c
LEFT JOIN pg_class t ON t.reltoastrelid = c.oid
LEFT JOIN pg_index i ON i.indexrelid = c.oid
LEFT JOIN pg_class ti ON ti.reltoastrelid = i.indrelid AND c.relkind = 'i'
WHERE pg_relation_filenode(c.oid) IS NOT NULL
"""

PG_BUFFERS = """
SELECT b.relfilenode::text, count(*) * current_setting('block_size')::int
FROM pg_buffercache b
WHERE b.reldatabase = (SELECT oid FROM pg_database WHERE datname = current_database())
GROUP BY 1
"""


def pg_components(table: str, files: list[dict]) -> list[dict]:
    with psycopg.connect(config.PG_DSN, autocommit=True) as c:
        c.execute("CREATE EXTENSION IF NOT EXISTS pg_buffercache")
        dboid = c.execute("SELECT oid::text FROM pg_database WHERE datname = current_database()").fetchone()[0]
        rels = {fn: (kind, name, size) for fn, kind, name, size in c.execute(PG_RELATIONS)}
        buffers = dict(c.execute(PG_BUFFERS).fetchall())
        shmem = c.execute("SELECT name, allocated_size FROM pg_shmem_allocations "
                          "WHERE allocated_size > 1048576 ORDER BY 2 DESC").fetchall()
    rows: dict[str, dict] = defaultdict(lambda: {"on_disk_mb": 0.0, "shared_buffers_mb": 0.0,
                                                 "page_cache_mb": 0.0})

    def bucket(fn: str) -> str:
        if fn not in rels:
            return "other"
        kind, name, _ = rels[fn]
        if not name.startswith(table):
            return "catalogs and other"
        return ("index " if kind == "i" else "table ") + name

    for fn, (kind, name, size) in rels.items():
        rows[bucket(fn)]["on_disk_mb"] += size / MB
        rows[bucket(fn)]["shared_buffers_mb"] += int(buffers.get(fn, 0)) / MB
    for f in files:  # base/<db oid>/<filenode>[.segment|_fsm|_vm]
        parts = f["path"].split("/")
        m = re.match(r"\d+", parts[2]) if len(parts) == 3 else None
        if m and parts[0] == "base" and parts[1] == dboid:
            rows[bucket(m.group())]["page_cache_mb"] += f["resident"] / MB
        else:
            rows["wal and other files"]["page_cache_mb"] += f["resident"] / MB
    out = [{"component": k, **{m: round(v, 1) for m, v in r.items()}} for k, r in sorted(rows.items())]
    out += [{"component": f"shmem {name}", "shared_buffers_mb": round(size / MB, 1)}
            for name, size in shmem]
    return out


# ---- Elasticsearch ---------------------------------------------------------------------------

LUCENE = {"tim": "terms", "tip": "terms", "tmd": "terms", "doc": "postings", "pos": "positions",
          "pay": "positions", "fdt": "stored fields", "fdx": "stored fields", "fdm": "stored fields",
          "dvd": "doc values", "dvm": "doc values", "nvd": "norms", "nvm": "norms",
          "kdd": "points", "kdi": "points", "kdm": "points", "cfs": "compound", "cfe": "compound"}


def es_components(files: list[dict]) -> list[dict]:
    rows: dict[str, dict] = defaultdict(lambda: {"on_disk_mb": 0.0, "page_cache_mb": 0.0})
    for f in files:
        ext = f["path"].rsplit(".", 1)[-1] if "/index/" in f["path"] else ""
        key = "lucene " + LUCENE.get(ext, "other") if ext else "translog and state"
        rows[key]["on_disk_mb"] += f["size"] / MB
        rows[key]["page_cache_mb"] += f["resident"] / MB
    node = next(iter(httpx.get(f"{config.ES_URL}/_nodes/stats/jvm,indices", timeout=30)
                     .json()["nodes"].values()))
    out = [{"component": k, **{m: round(v, 1) for m, v in r.items()}} for k, r in sorted(rows.items())]
    heap = node["jvm"]["mem"]
    out.append({"component": "jvm heap", "heap_used_mb": round(heap["heap_used_in_bytes"] / MB),
                "heap_max_mb": round(heap["heap_max_in_bytes"] / MB)})
    return out


# ---- measurements ----------------------------------------------------------------------------

def _snapshot(engine: str, subset: str, stage: str) -> None:
    service = service_of(engine)
    files = pagecache(volume_of(service), "resident")
    comps = (es_components(files) if service == "elasticsearch"
             else pg_components(config.table_name(subset), files))
    total = {"component": "container (cgroup)", **cgroup(service)}
    for comp in [*comps, total]:
        row = {"engine": engine, "subset": subset, "stage": stage, **comp}
        results.append("memory", row)
        print(row)


def working_set(engine: str, subset: str, facets: bool = True) -> None:
    service = service_of(engine)
    cold_start(service)
    _snapshot(engine, subset, "idle after start")
    latency(engine, subset, 10, 1, 3, label="memory-probe")
    _snapshot(engine, subset, "after top-10 queries")
    if facets:
        latency(engine, subset, 10, 1, 1, label="memory-probe", facets=True)
        _snapshot(engine, subset, "after facets")


def cold(engine: str, subset: str) -> None:
    """Cold start: nothing of the engine's data cached, then every query once (one client)."""
    cold_start(service_of(engine))
    latency(engine, subset, 10, 1, 1, label="cold")
    _snapshot(engine, subset, "after cold run")


def _measure_under(engine: str, subset: str, env: dict, label: str, facets: bool) -> None:
    service = service_of(engine)
    try:
        cold_start(service, env)
        latency(engine, subset, 10, 1, 1, label=f"{label} warmup")
        latency(engine, subset, 10, 1, 3, label=label)
        latency(engine, subset, 10, 16, 10, label=label, profile="website", rate=100)
        if facets:
            latency(engine, subset, 10, 1, 1, label=label, facets=True)
    except Exception as exc:  # an OOM kill or a stalled engine is a result, not a crash
        print(f"  {label}: {exc}")
        results.append("memory", {"engine": engine, "subset": subset, "stage": label,
                                  "component": "error", "error": str(exc)[:200]})
    try:
        _snapshot(engine, subset, label)
    except Exception as exc:
        print(f"  {label}: snapshot failed: {exc}")


def squeeze(engine: str, subset: str, mems: list[str], facets: bool = True) -> None:
    for mem in mems:
        b = _bytes(mem)
        if service_of(engine) == "elasticsearch":
            env = {"BENCH_MEM": mem, "ES_HEAP": f"{b // 2 // MB}m"}
            label = f"mem={mem} heap={env['ES_HEAP']}"
        else:
            env = {"BENCH_MEM": mem, "PG_SHARED_BUFFERS": f"{b // 4 // MB}MB",
                   "PG_EFFECTIVE_CACHE": f"{b * 3 // 4 // MB}MB"}
            label = f"mem={mem} sb={env['PG_SHARED_BUFFERS']}"
        _measure_under(engine, subset, env, label, facets)
    compose("up", "-d", service_of(engine))  # back to the .env settings
    wait_ready(service_of(engine))


def heap(engine: str, subset: str, heaps: list[str], facets: bool = True) -> None:
    if service_of(engine) != "elasticsearch":
        raise SystemExit("heap is for Elasticsearch arms")
    for h in heaps:
        _measure_under(engine, subset, {"ES_HEAP": h}, f"heap={h}", facets)
    compose("up", "-d", "elasticsearch")
    wait_ready("elasticsearch")
