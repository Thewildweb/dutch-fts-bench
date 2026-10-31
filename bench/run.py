"""Run the query set against one engine: result lists (for relevance) and latency.

Latency runs use one OS process per client, so the Python client is not the bottleneck.
Two modes:
- closed loop (default): each client sends its next query as soon as the previous returns.
  Good for max throughput; hides stalls in the tail.
- open loop (--rate R): queries are scheduled at R per second regardless of how fast the
  engine answers; latency counts from the scheduled time, so queueing shows up in p99.
"""
import json
import multiprocessing as mp
import subprocess
import random
import statistics
import time

from . import config, results, telemetry
from .engines import ENGINES
from .query import parse


def load_queries(profile: str | None = None) -> list[dict]:
    qs = [json.loads(l) for l in config.QUERIES.read_text().splitlines() if l.strip()]
    return [q for q in qs if profile in (None, q["profile"])]


def run_file(engine: str, subset: str):
    return config.RESULTS / "runs" / f"{engine}__{subset}.jsonl"


def result_lists(engine: str, subset: str, k: int = 100) -> dict:
    """Top-k ids and match count per query, written to results/runs/. Ids only, publishable."""
    e = ENGINES[engine](subset)
    path = run_file(engine, subset)
    path.parent.mkdir(parents=True, exist_ok=True)
    skipped = 0
    with path.open("w") as f:
        for q in load_queries():
            pq = parse(q["text"])
            reason = e.unsupported(pq, q["filters"])
            if reason:
                skipped += 1
                f.write(json.dumps({"qid": q["qid"], "unsupported": reason}) + "\n")
                continue
            ids = e.search(pq, q["filters"], k)
            f.write(json.dumps({"qid": q["qid"], "hits": e.count(pq, q["filters"]), "ids": ids}) + "\n")
    e.close()
    return {"file": str(path), "skipped": skipped}


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def _worker(args) -> tuple[list[float], list[str], float, float]:
    engine, subset, cache, k, facets, jobs, t0, interval = args
    e = ENGINES[engine](subset, cache=cache)
    lat, errors = [], []
    first = t0 if interval else time.perf_counter()
    for slot, q in jobs:
        if interval:  # open loop: wait for this job's scheduled time
            scheduled = t0 + slot * interval
            delay = scheduled - time.perf_counter()
            if delay > 0:
                # Idle client: count from the actual send, since sleep() can wake late
                # (~4 ms on macOS) and that lateness is the client's, not the engine's.
                time.sleep(delay)
                start = time.perf_counter()
            else:
                # Still busy with the previous query: the wait is queueing, so it counts.
                start = scheduled
        else:
            start = time.perf_counter()
        pq = parse(q["text"])
        try:
            if facets:
                e.facet_city(pq, q["filters"])
            else:
                e.search(pq, q["filters"], k)
        except Exception as exc:  # recorded, not fatal: one bad query shouldn't sink a run
            errors.append(f"{q['qid']}: {exc}")
            continue
        lat.append((time.perf_counter() - start) * 1000)
    end = time.perf_counter()
    e.close()
    return lat, errors, first, end


LOADGEN = config.ROOT / "loadgen" / "bin" / "loadgen"


def _go_clients(engine, subset, cache, k, facets, jobs, concurrency, rate, start_at):
    """Replay the same requests with the Go load generator; returns latencies like _worker."""
    if not LOADGEN.exists():
        raise SystemExit("loadgen not built: scripts/build-loadgen.sh")
    e = ENGINES[engine](subset, cache=cache)
    lines = [json.dumps(e.session())]
    lines += [json.dumps(e.request(parse(q["text"]), q["filters"], k, facets)) for _, q in jobs]
    e.close()
    cmd = [str(LOADGEN), "-concurrency", str(concurrency), "-start-at", f"{start_at:.6f}"]
    if rate:
        cmd += ["-rate", str(rate)]
    out = subprocess.run(cmd, input="\n".join(lines) + "\n", capture_output=True, text=True)
    if out.returncode:
        raise RuntimeError(f"loadgen failed: {out.stderr.strip()}")
    r = json.loads(out.stdout)
    return r["lat_ms"], r["errors"], r["first"], r["end"]


def _writer(args) -> int:
    """Copy random documents under new ids at `rate` per second until `stop_at`."""
    engine, subset, rate, t0, stop_at = args
    import duckdb
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    limit = config.SUBSETS[subset]
    sql = "SELECT id FROM read_parquet(?) ORDER BY md5(id)" + (f" LIMIT {limit}" if limit else "")
    ids = [r[0] for r in con.sql(sql, params=[str(config.DATASET_PATH)]).fetchall()]
    rnd = random.Random(7)
    e = ENGINES[engine](subset)
    n, next_at = 0, t0  # start with the queries, not during the start-up head start
    while next_at < stop_at:
        delay = next_at - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
        e.write_copy(rnd.choice(ids), f"w-{int(t0)}-{n}")
        n += 1
        next_at += 1 / rate
    e.close()
    return n


def _jobs(qs: list[dict], repeats: int, zipf: float | None, seed: int = 42) -> list[dict]:
    rnd = random.Random(seed)
    if zipf:
        # Popularity follows rank^-s over a fixed random order of the queries.
        order = qs[:]
        rnd.shuffle(order)
        weights = [1 / (r + 1) ** zipf for r in range(len(order))]
        return rnd.choices(order, weights=weights, k=repeats * len(qs))
    jobs = qs * repeats
    rnd.shuffle(jobs)
    return jobs


def latency(engine: str, subset: str, k: int, concurrency: int, repeats: int, label: str,
            profile: str | None = None, facets: bool = False, rate: float | None = None,
            zipf: float | None = None, cache: bool = False,
            writes: float | None = None, client: str = "go") -> list[dict]:
    """writes: documents copied per second during the run (needs --rate, so the run length
    is known); copies are removed afterwards. client: "go" (loadgen/, default) or "python"
    (one process per client). Both send the same requests; the Python clients used up to
    10 of 12 cores at 16 clients and added ~2 ms per Elasticsearch request."""
    if writes and not rate:
        raise SystemExit("--writes needs --rate, so the run has a known length")
    rows = []
    probe = ENGINES[engine](subset)
    for prof in ([profile] if profile else ["website", "recruiter"]):
        qs = [q for q in load_queries(prof) if not probe.unsupported(parse(q["text"]), q["filters"])]
        skipped = len(load_queries(prof)) - len(qs)
        if not qs:
            continue
        jobs = list(enumerate(_jobs(qs, repeats, zipf)))
        interval = 1 / rate if rate else None
        t0 = time.perf_counter() + 3.0  # let workers start and connect before the first scheduled slot
        # perf_counter is system-wide on Linux and macOS, so t0 is valid in child processes.
        parts = [(engine, subset, cache, k, facets, jobs[i::concurrency], t0, interval)
                 for i in range(concurrency)]
        run_name = f"{engine}__{subset}__{time.strftime('%Y%m%dT%H%M%S')}__{prof}"
        with telemetry.Sampler(telemetry.service_of(engine), run_name) as tel, \
                mp.get_context("spawn").Pool(
                    max(1, (concurrency if client == "python" else 0) + (1 if writes else 0))) as pool:
            writer = None
            if writes:
                stop_at = t0 + len(jobs) * interval
                writer = pool.apply_async(_writer, [(engine, subset, writes, t0, stop_at)])
            if client == "go":
                # Go's clock is wall time: map the perf_counter start to it.
                start_at = time.time() + (t0 - time.perf_counter())
                out = [_go_clients(engine, subset, cache, k, facets, jobs, concurrency, rate,
                                   start_at)]
            else:
                out = pool.map(_worker, parts)
            written = writer.get() if writer else 0
        if writes:
            cleanup = ENGINES[engine](subset)
            cleanup.remove_copies()
            cleanup.close()
        wall = max(o[3] for o in out) - min(o[2] for o in out)
        lat = [x for o in out for x in o[0]]
        errors = [x for o in out for x in o[1]]
        row = {"engine": engine, "subset": subset, "profile": prof,
               "op": "facet_city" if facets else f"top{k}", "concurrency": concurrency,
               "client": client,
               "mode": f"open@{rate}/s" if rate else "closed",
               "zipf": zipf or "", "cache": cache, "writes_per_s": writes or "",
               "written": written if writes else "", "label": label,
               "n": len(lat), "errors": len(errors), "skipped_queries": skipped,
               "p50_ms": round(_pct(lat, 50), 2), "p95_ms": round(_pct(lat, 95), 2),
               "p99_ms": round(_pct(lat, 99), 2), "max_ms": round(max(lat), 2),
               "mean_ms": round(statistics.mean(lat), 2), "qps": round(len(lat) / wall, 1),
               **tel.summary()}
        results.append("latency", row)
        rows.append(row)
        for err in errors[:5]:
            print("  error", err)
    probe.close()
    return rows
