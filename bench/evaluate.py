"""nDCG@10 and recall@100 per engine, profile and tag, from run files and qrels.

Unjudged documents count as non-relevant; the judged fraction of each top-10 is reported
so a low score from an engine that wasn't in the pool is visible. Recall is relative to
the relevant documents found in the judged pool (top-20 of every engine), not to the
whole collection. Queries an engine doesn't support are left out of its averages."""
import json
import math
from collections import defaultdict

from . import config, results
from .judge import load_qrels
from .run import load_queries


def ndcg(ids: list[str], rel: dict[str, int], k: int = 10) -> float:
    dcg = sum((2 ** rel.get(d, 0) - 1) / math.log2(i + 2) for i, d in enumerate(ids[:k]))
    ideal = sorted(rel.values(), reverse=True)[:k]
    idcg = sum((2 ** g - 1) / math.log2(i + 2) for i, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def evaluate(subset: str) -> list[dict]:
    qrels = defaultdict(dict)
    for (qid, i), g in load_qrels().items():
        qrels[qid][i] = g
    queries = {q["qid"]: q for q in load_queries()}
    rows = []
    for path in sorted((config.RESULTS / "runs").glob(f"*__{subset}.jsonl")):
        engine = path.name.split("__")[0]
        groups: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
        for line in path.read_text().splitlines():
            r = json.loads(line)
            if "unsupported" in r:
                continue  # reported via the run file, not scored as zero
            rel = qrels.get(r["qid"], {})
            if not any(rel.values()):
                continue  # no relevant doc found by any engine: nothing to measure
            relevant = {d for d, g in rel.items() if g > 0}
            score = (ndcg(r["ids"], rel),
                     len(relevant & set(r["ids"][:100])) / len(relevant),
                     sum(d in rel for d in r["ids"][:10]) / max(1, min(10, len(r["ids"]))))
            q = queries[r["qid"]]
            for key in ["all", q["profile"], *(f"tag:{t}" for t in q["tags"])]:
                groups[key].append(score)
        for key, scores in sorted(groups.items()):
            row = {"engine": engine, "subset": subset, "group": key, "queries": len(scores),
                   "ndcg10": round(sum(s[0] for s in scores) / len(scores), 3),
                   "recall100": round(sum(s[1] for s in scores) / len(scores), 3),
                   "judged10": round(sum(s[2] for s in scores) / len(scores), 3)}
            rows.append(row)
    for row in rows:
        results.append("relevance", row)
    return rows
