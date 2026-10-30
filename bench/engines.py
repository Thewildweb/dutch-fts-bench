"""Search arms. Every arm takes the same parsed Query and filters and returns ids, best first.

filters: {"lang": "nl", "city": "Utrecht", "since": "2026-09-01"} (all optional).
"""
import httpx
import psycopg

from . import config
from .query import Clause, Query, to_tantivy, to_tsquery_sql

TITLE_BOOST = 2.0


class Engine:
    name = "base"

    def __init__(self, subset: str):
        self.subset = subset
        self.table = config.table_name(subset)

    def search(self, q: Query, filters: dict, k: int) -> list[str]:
        raise NotImplementedError

    def facet_city(self, q: Query, filters: dict, n: int = 10) -> list[tuple[str, int]]:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- Postgres

class _Pg(Engine):
    def __init__(self, subset: str):
        super().__init__(subset)
        self.conn = psycopg.connect(config.PG_DSN, autocommit=True)

    def close(self) -> None:
        self.conn.close()

    @staticmethod
    def _filters(filters: dict) -> tuple[str, list]:
        sql, args = [], []
        if "lang" in filters:
            sql.append("lang = %s"); args.append(filters["lang"])
        if "city" in filters:
            sql.append("city = %s"); args.append(filters["city"])
        if "since" in filters:
            sql.append("last_seen >= %s"); args.append(filters["since"])
        return (" AND ".join(sql) or "TRUE"), args

    def _where(self, q: Query) -> tuple[str, list]:
        raise NotImplementedError

    def _rows(self, q: Query, filters: dict, k: int) -> tuple[str, list]:
        raise NotImplementedError

    def search(self, q, filters, k):
        sql, args = self._rows(q, filters, k)
        return [r[0] for r in self.conn.execute(sql, args)]

    def facet_city(self, q, filters, n=10):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        sql = (f"SELECT city, count(*) FROM {self.table} WHERE {where} AND {fsql} AND city <> '' "
               f"GROUP BY city ORDER BY 2 DESC LIMIT {n}")
        return [tuple(r) for r in self.conn.execute(sql, args + fargs)]


class PgIlike(_Pg):
    """Naive baseline: substring match, no ranking beyond 'term in title' then recency.

    Run on the same table with and without the pg_trgm indexes (arm names ilike / trgm)."""
    name = "pg-ilike"
    use_indexes = False  # plain ILIKE = sequential scan, even when the trgm indexes exist

    def __init__(self, subset: str):
        super().__init__(subset)
        if not self.use_indexes:
            self.conn.execute("SET enable_bitmapscan = off")
            self.conn.execute("SET enable_indexscan = off")

    @staticmethod
    def _pattern(c: Clause) -> str:
        v = c.text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return f"%{v}%"

    def _where(self, q):
        parts, args = [], []
        for group in q.must:
            parts.append("(" + " OR ".join("(title ILIKE %s OR job_text ILIKE %s)" for _ in group) + ")")
            for c in group:
                args += [self._pattern(c)] * 2
        for c in q.must_not:
            parts.append("NOT (title ILIKE %s OR job_text ILIKE %s)")
            args += [self._pattern(c)] * 2
        return " AND ".join(parts) or "TRUE", args

    def _rows(self, q, filters, k):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        pats = [self._pattern(c) for c in (g[0] for g in q.must)]
        title_hits = " + ".join("(title ILIKE %s)::int" for _ in pats) or "0"
        sql = (f"SELECT id FROM {self.table} WHERE {where} AND {fsql} "
               f"ORDER BY {title_hits} DESC, last_seen DESC, id LIMIT {k}")
        return sql, args + fargs + pats


class PgTrgm(PgIlike):
    name = "pg-trgm"
    use_indexes = True


class PgFts(_Pg):
    """Built-in full-text search: tsvector per document language, GIN index, ts_rank_cd."""
    name = "pg-fts"
    CONFIGS = (("nl", "dutch"), ("en", "english"), (None, "simple"))

    def _tsq(self, q: Query) -> dict[str, tuple[str, list]]:
        return {cfg: to_tsquery_sql(q, cfg) for _, cfg in self.CONFIGS}

    def _where(self, q):
        parts, args = [], []
        for lang, cfg in self.CONFIGS:
            expr, a = self._tsq(q)[cfg]
            cond = f"lang = '{lang}'" if lang else "lang NOT IN ('nl', 'en')"
            parts.append(f"({cond} AND tsv @@ {expr})")
            args += a
        return "(" + " OR ".join(parts) + ")", args

    def _rows(self, q, filters, k):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        tsq = self._tsq(q)
        (nl, a1), (en, a2), (xx, a3) = tsq["dutch"], tsq["english"], tsq["simple"]
        rank = f"ts_rank_cd(tsv, CASE lang WHEN 'nl' THEN {nl} WHEN 'en' THEN {en} ELSE {xx} END)"
        sql = (f"SELECT id FROM {self.table} WHERE {where} AND {fsql} "
               f"ORDER BY {rank} DESC, id LIMIT {k}")
        return sql, args + fargs + a1 + a2 + a3


class PgSearch(_Pg):
    """ParadeDB pg_search: BM25 (Tantivy) inside Postgres."""
    name = "pg-search"
    FIELDS = [f"title_{l}^{TITLE_BOOST}" for l in ("nl", "en", "xx")] + \
             [f"text_{l}" for l in ("nl", "en", "xx")]

    def _where(self, q):
        return "id @@@ pdb.parse(%s)", [to_tantivy(q, self.FIELDS)]

    def _rows(self, q, filters, k):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        sql = (f"SELECT id FROM {self.table} WHERE {where} AND {fsql} "
               f"ORDER BY pdb.score(id) DESC, id LIMIT {k}")
        return sql, args + fargs


# ----------------------------------------------------------- Elasticsearch

class Elastic(Engine):
    name = "elasticsearch"
    FIELDS = [f"title_{l}^{TITLE_BOOST}" for l in ("nl", "en", "xx")] + \
             [f"text_{l}" for l in ("nl", "en", "xx")]

    def __init__(self, subset: str):
        super().__init__(subset)
        self.index = self.table
        self.http = httpx.Client(base_url=config.ES_URL, timeout=60)

    def close(self) -> None:
        self.http.close()

    def _clause(self, c: Clause) -> dict:
        return {"multi_match": {"query": c.text, "fields": self.FIELDS,
                                "type": "phrase" if c.phrase else "best_fields",
                                "operator": "and"}}

    def _query(self, q: Query, filters: dict) -> dict:
        must = [{"bool": {"should": [self._clause(c) for c in g], "minimum_should_match": 1}}
                for g in q.must]
        flt = []
        if "lang" in filters:
            flt.append({"term": {"lang": filters["lang"]}})
        if "city" in filters:
            flt.append({"term": {"city": filters["city"]}})
        if "since" in filters:
            flt.append({"range": {"last_seen": {"gte": filters["since"]}}})
        return {"bool": {"must": must, "must_not": [self._clause(c) for c in q.must_not],
                         "filter": flt}}

    def search(self, q, filters, k):
        body = {"query": self._query(q, filters), "size": k, "_source": False,
                "track_total_hits": False, "sort": ["_score", {"id": "asc"}]}
        r = self.http.post(f"/{self.index}/_search", json=body)
        r.raise_for_status()
        return [h["_id"] for h in r.json()["hits"]["hits"]]

    def facet_city(self, q, filters, n=10):
        body = {"query": self._query(q, filters), "size": 0,
                "aggs": {"city": {"terms": {"field": "city", "size": n,
                                            "exclude": [""]}}}}
        r = self.http.post(f"/{self.index}/_search", json=body)
        r.raise_for_status()
        return [(b["key"], b["doc_count"]) for b in r.json()["aggregations"]["city"]["buckets"]]


ENGINES = {e.name: e for e in (PgIlike, PgTrgm, PgFts, PgSearch, Elastic)}
