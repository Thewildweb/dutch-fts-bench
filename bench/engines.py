"""Search arms. Every arm takes the same parsed Query and filters and returns ids, best first.

filters: {"lang": "nl", "city": "Utrecht", "since": "2026-09-01"} (all optional).
An arm that can't run a query type says so in `unsupported()`; such queries are skipped and
reported, never silently simplified.
"""
import httpx
import psycopg

from . import config
from .query import Clause, Query, to_tantivy, to_tsquery_sql

TITLE_BOOST = 2.0
LANGS = (("nl", "dutch"), ("en", "english"), (None, "simple"))  # None = every other lang


def lang_cond(lang: str | None) -> str:
    return f"lang = '{lang}'" if lang else "lang NOT IN ('nl', 'en')"


class Engine:
    name = "base"

    def __init__(self, subset: str, cache: bool = False):
        self.subset = subset
        self.table = config.table_name(subset)
        self.cache = cache

    def unsupported(self, q: Query, filters: dict) -> str | None:
        return None

    def search(self, q: Query, filters: dict, k: int) -> list[str]:
        raise NotImplementedError

    def count(self, q: Query, filters: dict) -> int:
        raise NotImplementedError

    def facet_city(self, q: Query, filters: dict, n: int = 10) -> list[tuple[str, int]]:
        raise NotImplementedError

    # Write load for cache runs: copy an existing document under a new id ("w-..."), then
    # remove all copies afterwards so the next run sees the original dataset.
    def write_copy(self, src_id: str, new_id: str) -> None:
        raise NotImplementedError

    def remove_copies(self) -> int:
        raise NotImplementedError

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- Postgres

class _Pg(Engine):
    def __init__(self, subset: str, cache: bool = False):
        super().__init__(subset, cache)
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

    def count(self, q, filters):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        return self.conn.execute(f"SELECT count(*) FROM {self.table} WHERE {where} AND {fsql}",
                                 args + fargs).fetchone()[0]

    def facet_city(self, q, filters, n=10):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        sql = (f"SELECT city, count(*) FROM {self.table} WHERE {where} AND {fsql} AND city <> '' "
               f"GROUP BY city ORDER BY 2 DESC LIMIT {n}")
        return [tuple(r) for r in self.conn.execute(sql, args + fargs)]

    _COLS = ("url, domain, title, lang, job_text, text_length, word_count, city, "
             "first_seen, last_seen")

    def write_copy(self, src_id, new_id):
        # Generated columns (tsv, doc_*) and trigger-filled token columns update themselves.
        self.conn.execute(f"INSERT INTO {self.table} (id, {self._COLS}) "
                          f"SELECT %s, {self._COLS} FROM {self.table} WHERE id = %s",
                          [new_id, src_id])

    def remove_copies(self):
        n = self.conn.execute(f"DELETE FROM {self.table} WHERE id LIKE 'w-%%'").rowcount
        # Force index cleanup: with few dead rows, plain VACUUM skips the indexes ("index
        # scan bypassed"), which left deleted copies inside the pg_search index.
        self.conn.execute(f"VACUUM (INDEX_CLEANUP ON, ANALYZE) {self.table}")
        return n


class PgIlike(_Pg):
    """Naive baseline: substring match, no ranking beyond 'term in title' then recency.

    Run on the same table with and without the pg_trgm indexes (arm names ilike / trgm)."""
    name = "pg-ilike"
    use_indexes = False  # plain ILIKE = sequential scan, even when the trgm indexes exist

    def __init__(self, subset: str, cache: bool = False):
        super().__init__(subset, cache)
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
    # ts_rank_cd normalization flags; 0 = default (no length normalization).
    normalization = 0
    # Weights for D, C, B, A labels. Title is A, body B: A/B = 2, the same title boost as
    # pg_search and Elasticsearch (the default {0.1, 0.2, 0.4, 1.0} would make it 2.5).
    WEIGHTS = "{0.1, 0.2, 0.5, 1.0}"

    def _tsq(self, q: Query) -> dict[str, tuple[str, list]]:
        return {cfg: to_tsquery_sql(q, cfg) for _, cfg in LANGS}

    def _where(self, q):
        parts, args = [], []
        tsq = self._tsq(q)
        for lang, cfg in LANGS:
            expr, a = tsq[cfg]
            parts.append(f"({lang_cond(lang)} AND tsv @@ {expr})")
            args += a
        return "(" + " OR ".join(parts) + ")", args

    def _rows(self, q, filters, k):
        where, args = self._where(q)
        fsql, fargs = self._filters(filters)
        tsq = self._tsq(q)
        (nl, a1), (en, a2), (xx, a3) = tsq["dutch"], tsq["english"], tsq["simple"]
        rank = (f"ts_rank_cd('{self.WEIGHTS}', tsv, CASE lang WHEN 'nl' THEN {nl} WHEN 'en' THEN {en} ELSE {xx} END, "
                f"{self.normalization})")
        sql = (f"SELECT id FROM {self.table} WHERE {where} AND {fsql} "
               f"ORDER BY {rank} DESC, id LIMIT {k}")
        return sql, args + fargs + a1 + a2 + a3


class PgFtsNorm(PgFts):
    """Same, with ranking divided by 1 + log(document length): the usual tuning step."""
    name = "pg-fts-norm"
    normalization = 1


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


class PgSearchNoBoost(PgSearch):
    """Same index, no title boost: separates the effect of the boost from the BM25 engine."""
    name = "pg-search-noboost"
    FIELDS = [f"title_{l}" for l in ("nl", "en", "xx")] + [f"text_{l}" for l in ("nl", "en", "xx")]


class PgTextsearch(PgFts):
    """Tiger Data pg_textsearch: BM25 top-k from one partial index per language.

    Its ranking query matches *any* term, so the same AND/phrase/exclude semantics as the
    other arms come from the stored tsvector column (cheap per-row check, no GIN needed).
    Title and body are indexed as one text, so there is no title boost."""
    name = "pg-textsearch"
    INDEX = "bm25"  # index names {table}_{INDEX}_{lang}

    def doc(self, lang: str | None) -> str:
        """Must repeat the index expression exactly (see sql/pg_indexes.sql)."""
        return f"(CASE WHEN {lang_cond(lang)} THEN title || E'\\n' || job_text END)"

    def _rows(self, q, filters, k):
        fsql, fargs = self._filters(filters)
        tsq = self._tsq(q)
        terms = " ".join(q.terms())
        parts, args = [], []
        for lang, cfg in LANGS:
            expr, a = tsq[cfg]
            idx = f"{self.table}_{self.INDEX}_{lang or 'xx'}"
            doc = self.doc(lang)
            parts.append(
                f"(SELECT id, {doc} <@> to_bm25query(%s, '{idx}') AS s FROM {self.table} "
                f"WHERE {lang_cond(lang)} AND tsv @@ {expr} AND {fsql} "
                f"ORDER BY {doc} <@> to_bm25query(%s, '{idx}') LIMIT {k})")
            args += [terms] + a + fargs + [terms]
        sql = f"SELECT id FROM ({' UNION ALL '.join(parts)}) t ORDER BY s, id LIMIT {k}"
        return sql, args


class PgTextsearchTuned(PgTextsearch):
    """Same, with enable_sort = off. The planner underestimates how many rows pass the tsv
    check and then scores matching rows one by one instead of walking the BM25 index in
    score order (10k sample: p50 9.7 ms -> 0.7 ms, same results)."""
    name = "pg-textsearch-tuned"

    def __init__(self, subset: str, cache: bool = False):
        super().__init__(subset, cache)
        self.conn.execute("SET enable_sort = off")


class PgTextsearchTitle(PgTextsearchTuned):
    """Workaround for the missing field boost: the title is indexed twice, then the body.
    Raises the title's term counts (BM25 dampens those) and lengthens the document, so it
    is not the same as a ×2 field boost."""
    name = "pg-textsearch-title"
    INDEX = "bm25t"

    def doc(self, lang):
        return (f"(CASE WHEN {lang_cond(lang)} THEN title || E'\\n' || title || E'\\n' || "
                f"job_text END)")


# ----------------------------------------------------------- Elasticsearch

class Elastic(Engine):
    """cache=False sends request_cache=false, so repeated size:0 requests (counts, facets)
    measure engine work rather than the shard request cache; cache=True is the default
    behaviour."""
    name = "elasticsearch"
    TYPE = "best_fields"  # score = best single field (title or body)
    FIELDS = [f"title_{l}^{TITLE_BOOST}" for l in ("nl", "en", "xx")] + \
             [f"text_{l}" for l in ("nl", "en", "xx")]

    def __init__(self, subset: str, cache: bool = False):
        super().__init__(subset, cache)
        self.index = self.table
        self.http = httpx.Client(base_url=config.ES_URL, timeout=60)
        # cache=True: Elasticsearch's default (the shard request cache serves size:0 requests
        # such as counts and facets, not hit lists). cache=False: request_cache=false.
        self.params = {} if cache else {"request_cache": "false"}

    def close(self) -> None:
        self.http.close()

    def _clause(self, c: Clause) -> dict:
        return {"multi_match": {"query": c.text, "fields": self.FIELDS,
                                "type": "phrase" if c.phrase else self.TYPE,
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

    def _search(self, body: dict) -> dict:
        r = self.http.post(f"/{self.index}/_search", json=body, params=self.params)
        r.raise_for_status()
        return r.json()

    def search(self, q, filters, k):
        body = {"query": self._query(q, filters), "size": k, "_source": False,
                "track_total_hits": False, "sort": ["_score", {"id": "asc"}]}
        return [h["_id"] for h in self._search(body)["hits"]["hits"]]

    def count(self, q, filters):
        body = {"query": self._query(q, filters), "size": 0, "track_total_hits": True}
        return self._search(body)["hits"]["total"]["value"]

    def write_copy(self, src_id, new_id):
        doc = self.http.get(f"/{self.index}/_doc/{src_id}").json()["_source"]
        doc["id"] = new_id
        self.http.put(f"/{self.index}/_doc/{new_id}", json=doc).raise_for_status()

    def remove_copies(self):
        # Refresh first: copies written in the last second aren't searchable yet.
        self.http.post(f"/{self.index}/_refresh").raise_for_status()
        r = self.http.post(f"/{self.index}/_delete_by_query", params={"refresh": "true"},
                           json={"query": {"prefix": {"id": "w-"}}}, timeout=600)
        r.raise_for_status()
        return r.json()["deleted"]

    def facet_city(self, q, filters, n=10):
        body = {"query": self._query(q, filters), "size": 0, "track_total_hits": False,
                "aggs": {"city": {"terms": {"field": "city", "size": n, "exclude": [""]}}}}
        return [(b["key"], b["doc_count"]) for b in self._search(body)["aggregations"]["city"]["buckets"]]


class ElasticMostFields(Elastic):
    """Per term, sums the title and body scores (as pg_search's OR over fields does)
    instead of taking the best field."""
    name = "elasticsearch-mostfields"
    TYPE = "most_fields"


ENGINES = {e.name: e for e in (PgIlike, PgTrgm, PgFts, PgFtsNorm, PgSearch, PgSearchNoBoost,
                                PgTextsearch, PgTextsearchTuned, PgTextsearchTitle, Elastic, ElasticMostFields)}
