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
import random
import statistics
import time

from . import config, results
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
            zipf: float | None = None, cache: bool = False) -> list[dict]:
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
        with mp.get_context("spawn").Pool(concurrency) as pool:
            out = pool.map(_worker, parts)
        wall = max(o[3] for o in out) - min(o[2] for o in out)
        lat = [x for o in out for x in o[0]]
        errors = [x for o in out for x in o[1]]
        row = {"engine": engine, "subset": subset, "profile": prof,
               "op": "facet_city" if facets else f"top{k}", "concurrency": concurrency,
               "mode": f"open@{rate}/s" if rate else "closed",
               "zipf": zipf or "", "cache": cache, "label": label,
               "n": len(lat), "errors": len(errors), "skipped_queries": skipped,
               "p50_ms": round(_pct(lat, 50), 2), "p95_ms": round(_pct(lat, 95), 2),
               "p99_ms": round(_pct(lat, 99), 2), "max_ms": round(max(lat), 2),
               "mean_ms": round(statistics.mean(lat), 2), "qps": round(len(lat) / wall, 1)}
        results.append("latency", row)
        rows.append(row)
        for err in errors[:5]:
            print("  error", err)
    probe.close()
    return rows
