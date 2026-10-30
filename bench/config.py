import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise SystemExit(f"missing setting {name}; copy .env.example to .env")
    return value


DATASET_PATH = ROOT / env("DATASET_PATH", "data/rje-jobs-2026-09.parquet")
DATASET_SHA256 = env("DATASET_SHA256", "")
PG_DSN = env("PG_DSN", "postgresql://bench:bench@127.0.0.1:5432/bench")
ES_URL = env("ES_URL", "http://127.0.0.1:9200")
JUDGE_URL = env("JUDGE_URL", "http://127.0.0.1:8000/v1")
JUDGE_MODEL = env("JUDGE_MODEL", "gemma")

RESULTS = ROOT / "results"
QUERIES = ROOT / "queries" / "queries.jsonl"
JUDGMENTS = ROOT / "judgments"

# Subsets are deterministic: rows ordered by md5(id), first N taken.
SUBSETS = {"full": None, "10k": 10_000}


def table_name(subset: str) -> str:
    return "jobs" if subset == "full" else f"jobs_{subset}"
