"""bench: command line for the benchmark. Run `uv run bench --help`."""
import argparse
import json

from . import config, dataset, evaluate, indexes, judge, load_pg, results, run
from .engines import ENGINES


def main() -> None:
    p = argparse.ArgumentParser(prog="bench")
    p.add_argument("--subset", choices=list(config.SUBSETS), default="10k")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("verify", help="check the dataset checksum")
    s = sub.add_parser("load", help="load rows into an engine (idempotent)")
    s.add_argument("target", choices=["pg", "es"])
    s = sub.add_parser("index", help="build a Postgres index arm")
    s.add_argument("arm", choices=indexes.PG_ARMS)
    s = sub.add_parser("drop", help="drop a Postgres index arm")
    s.add_argument("arm", choices=indexes.PG_ARMS)
    s = sub.add_parser("runs", help="write top-100 result lists for relevance")
    s.add_argument("engine", choices=list(ENGINES))
    s = sub.add_parser("latency", help="measure latency, appended to results/latency.csv")
    s.add_argument("engine", choices=list(ENGINES))
    s.add_argument("--k", type=int, default=10)
    s.add_argument("--concurrency", type=int, nargs="+", default=[1, 4, 16])
    s.add_argument("--repeats", type=int, default=5)
    s.add_argument("--label", default="warm")
    s.add_argument("--profile", choices=["website", "recruiter"])
    s.add_argument("--facets", action="store_true", help="time the city facet count instead")
    s.add_argument("--rate", type=float, help="open loop: queries per second (default: closed loop)")
    s.add_argument("--zipf", type=float, help="skewed query popularity, e.g. 1.0 (default: uniform)")
    s.add_argument("--cache", action="store_true", help="allow engine result caches (ES request cache)")
    s.add_argument("--writes", type=float, help="copy N docs/s during the run (needs --rate)")
    s = sub.add_parser("judge", help="LLM-grade the pooled top-20s")
    s.add_argument("--limit", type=int)
    s.add_argument("--workers", type=int, default=1, help="parallel requests to the grader")
    s = sub.add_parser("calibrate", help="print graded pairs for a human check")
    s.add_argument("-n", type=int, default=20)
    sub.add_parser("eval", help="nDCG@10 / recall@100, appended to results/relevance.csv")

    a = p.parse_args()
    if a.cmd == "verify":
        dataset.verify(); print("ok")
    elif a.cmd == "load" and a.target == "pg":
        r = load_pg.load(a.subset)
        results.append("index", {"engine": "postgres", "arm": "load", "subset": a.subset,
                                 "seconds": r["seconds"], "index_bytes": 0,
                                 "table_growth_bytes": 0, "detail": json.dumps(r)})
        print(r)
    elif a.cmd == "load":
        print(indexes.build_es(a.subset))
    elif a.cmd == "index":
        print(indexes.build_pg(a.arm, a.subset))
    elif a.cmd == "drop":
        indexes.drop_pg(a.arm, a.subset)
    elif a.cmd == "runs":
        print(run.result_lists(a.engine, a.subset))
    elif a.cmd == "latency":
        for c in a.concurrency:
            for r in run.latency(a.engine, a.subset, a.k, c, a.repeats, a.label,
                                 a.profile, a.facets, a.rate, a.zipf, a.cache, a.writes):
                print(r)
    elif a.cmd == "judge":
        print("graded", judge.judge(a.subset, a.limit, a.workers))
    elif a.cmd == "calibrate":
        print(judge.calibration_sample(a.n))
    elif a.cmd == "eval":
        for r in evaluate.evaluate(a.subset):
            if not r["group"].startswith("tag:"):
                print(r)
