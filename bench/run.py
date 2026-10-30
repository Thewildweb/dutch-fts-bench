"""Run the query set against one engine: result lists (for relevance) and latency."""
import json
import queue
import random
import statistics
import threading
import time

from . import config, results
from .engines import ENGINES
from .query import parse


def load_queries(profile: str | None = None) -> list[dict]:
    qs = [json.loads(l) for l in config.QUERIES.read_text().splitlines() if l.strip()]
    return [q for q in qs if profile in (None, q["profile"])]


def run_file(engine: str, subset: str):
    return config.RESULTS / "runs" / f"{engine}__{subset}.jsonl"


def result_lists(engine: str, subset: str, k: int = 100) -> None:
    """Top-k ids per query, written to results/runs/. Ids only, so the file can be published."""
    e = ENGINES[engine](subset)
    path = run_file(engine, subset)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as f:
        for q in load_queries():
            ids = e.search(parse(q["text"]), q["filters"], k)
            f.write(json.dumps({"qid": q["qid"], "ids": ids}) + "\n")
    e.close()


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def latency(engine: str, subset: str, k: int, concurrency: int, repeats: int,
            label: str, profile: str | None = None, facets: bool = False) -> list[dict]:
    """Every query `repeats` times, shuffled, spread over `concurrency` clients.

    label: free tag such as 'warm' or 'cold' (cold = restart the engine, drop OS caches,
    then run with --repeats 1)."""
    rows = []
    for prof in ([profile] if profile else ["website", "recruiter"]):
        qs = load_queries(prof)
        jobs = [(q, parse(q["text"])) for q in qs] * repeats
        random.Random(42).shuffle(jobs)
        todo: queue.Queue = queue.Queue()
        for j in jobs:
            todo.put(j)
        lat: list[float] = []
        errors: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            e = ENGINES[engine](subset)
            mine = []
            while True:
                try:
                    q, pq = todo.get_nowait()
                except queue.Empty:
                    break
                t = time.perf_counter()
                try:
                    if facets:
                        e.facet_city(pq, q["filters"])
                    else:
                        e.search(pq, q["filters"], k)
                except Exception as exc:  # recorded, not fatal: one bad query shouldn't sink a run
                    errors.append(f"{q['qid']}: {exc}")
                    continue
                mine.append((time.perf_counter() - t) * 1000)
            e.close()
            with lock:
                lat.extend(mine)

        started = time.perf_counter()
        threads = [threading.Thread(target=worker) for _ in range(concurrency)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.perf_counter() - started
        row = {"engine": engine, "subset": subset, "profile": prof,
               "op": "facet_city" if facets else f"top{k}", "concurrency": concurrency,
               "label": label, "n": len(lat), "errors": len(errors),
               "p50_ms": round(_pct(lat, 50), 2), "p95_ms": round(_pct(lat, 95), 2),
               "p99_ms": round(_pct(lat, 99), 2), "mean_ms": round(statistics.mean(lat), 2),
               "qps": round(len(lat) / wall, 1)}
        results.append("latency", row)
        rows.append(row)
        for err in errors[:5]:
            print("  error", err)
    return rows
