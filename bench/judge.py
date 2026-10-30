"""Pool the top-20 of every engine per query and grade each (query, doc) pair 0/1/2 with an LLM.

Grades are cached in judgments/qrels.jsonl, so re-runs only grade new pairs."""
import json
import re

import duckdb
import httpx

from . import config
from .run import load_queries

POOL_DEPTH = 20  # judged depth per engine; deeper pools make recall@100 less biased
QRELS = config.JUDGMENTS / "qrels.jsonl"

PROMPT = """You grade search results for a Dutch job-search engine.

Search query: {query}
{filters}
Job posting
Title: {title}
City: {city}
Text (truncated):
{text}

How well does this posting match what the searcher is looking for?
2 = clearly relevant: this is the kind of job the query asks for (right role or skill; right place if one is named)
1 = partly relevant: related role, or the right role but a mismatch on a stated detail
0 = not relevant: the query words appear but the job is something else

Answer with one digit only."""


def load_qrels() -> dict[tuple[str, str], int]:
    if not QRELS.exists():
        return {}
    out = {}
    for line in QRELS.read_text().splitlines():
        r = json.loads(line)
        out[(r["qid"], r["id"])] = r["grade"]
    return out


def pool(subset: str) -> dict[str, set[str]]:
    pooled: dict[str, set[str]] = {}
    for path in (config.RESULTS / "runs").glob(f"*__{subset}.jsonl"):
        for line in path.read_text().splitlines():
            r = json.loads(line)
            pooled.setdefault(r["qid"], set()).update(r.get("ids", [])[:POOL_DEPTH])
    return pooled


def _docs(ids: set[str]) -> dict[str, dict]:
    con = duckdb.connect()
    con.execute("SET enable_progress_bar = false")
    con.execute("CREATE TEMP TABLE want(id VARCHAR)")
    con.executemany("INSERT INTO want VALUES (?)", [[i] for i in ids])
    rows = con.sql("SELECT id, title, city, jobText FROM read_parquet(?) JOIN want USING (id)",
                   params=[str(config.DATASET_PATH)]).fetchall()
    return {r[0]: {"title": r[1], "city": r[2], "text": r[3]} for r in rows}


def grade(client: httpx.Client, q: dict, doc: dict) -> int:
    filters = ("Filters applied: " + json.dumps(q["filters"], ensure_ascii=False) + "\n") if q["filters"] else ""
    prompt = PROMPT.format(query=q["text"], filters=filters, title=doc["title"],
                           city=doc["city"] or "-", text=doc["text"][:2500])
    r = client.post("/chat/completions", json={
        "model": config.JUDGE_MODEL, "temperature": 0, "max_tokens": 4,
        "messages": [{"role": "user", "content": prompt}],
        "chat_template_kwargs": {"enable_thinking": False}})
    r.raise_for_status()
    m = re.search(r"[012]", r.json()["choices"][0]["message"]["content"])
    if not m:
        raise ValueError(f"unparseable grade: {r.json()['choices'][0]['message']['content']!r}")
    return int(m.group())


def judge(subset: str, limit: int | None = None) -> int:
    queries = {q["qid"]: q for q in load_queries()}
    done = load_qrels()
    todo = [(qid, i) for qid, ids in sorted(pool(subset).items()) for i in sorted(ids)
            if (qid, i) not in done]
    todo = todo[:limit] if limit else todo
    docs = _docs({i for _, i in todo})
    QRELS.parent.mkdir(parents=True, exist_ok=True)
    with httpx.Client(base_url=config.JUDGE_URL, timeout=120) as client, QRELS.open("a") as f:
        for n, (qid, i) in enumerate(todo, 1):
            g = grade(client, queries[qid], docs[i])
            f.write(json.dumps({"qid": qid, "id": i, "grade": g, "by": config.JUDGE_MODEL}) + "\n")
            f.flush()
            if n % 100 == 0:
                print(f"  graded {n}/{len(todo)}")
    return len(todo)


def calibration_sample(n: int = 20, seed: int = 7) -> str:
    """Markdown table of graded pairs for a human to check before trusting the grader."""
    import random
    qrels = list(load_qrels().items())
    random.Random(seed).shuffle(qrels)
    by_grade = {g: [p for p in qrels if p[1] == g] for g in (0, 1, 2)}
    picked = [p for g in (0, 1, 2) for p in by_grade[g][: n // 3 + 1]][:n]
    queries = {q["qid"]: q for q in load_queries()}
    docs = _docs({i for (_, i), _ in picked})
    out = ["| qid | query | title | city | LLM grade | your grade |", "|---|---|---|---|---|---|"]
    for (qid, i), g in picked:
        d = docs[i]
        out.append(f"| {qid} | {queries[qid]['text']} | {d['title'][:60]} | {d['city']} | {g} | |")
    return "\n".join(out)
