# dutch-fts-bench

Full-text search benchmark on ~465k real Dutch job postings, comparing Postgres options
(plain `ILIKE`, `pg_trgm`, built-in `tsvector`, ParadeDB `pg_search`) and dedicated engines
(Elasticsearch first).

Two use cases:
- **website**: short queries from a job-site search box, many users. Target p99 < 100 ms.
- **recruiter**: detailed searches with phrases, `OR`, exclusions and filters on city, language and date.

Relevance and speed count equally. Vector/hybrid search is out of scope for now.

## Dataset

The dataset is **not** part of this repository. Results, queries and relevance judgments
are published and refer to documents by `id` only.

465,385 job postings collected June–September 2026, one row per distinct text. Columns:
`id, url, domain, title, lang, jobText, textLength, wordCount, city, firstSeen, lastSeen`.
Language mix: nl 435k, en 28k, de 1.3k, other small. Set `DATASET_PATH` and `DATASET_SHA256`
in `.env`; `bench verify` checks the file.

## Fairness rules

- Every engine loads the identical file, and ids are compared across engines.
- Analyzer per document from `lang`: `nl` → Dutch stemming, `en` → English, other → no stemming.
  Markdown headings in the text are indexed as-is everywhere.
- One query syntax (`bench/query.py`), parsed once and rendered per engine:
  terms are ANDed, `"phrases"`, `a OR b`, `-exclude`. A term may match title or body;
  title matches get a ×2 boost.
- Same CPU and memory limits for every container (`BENCH_CPUS`, `BENCH_MEM`), one engine running at a time.

## Running

```bash
cp .env.example .env            # set paths, ports, limits
uv sync
uv run bench verify

# Postgres (ParadeDB image; pg-textsearch and pg-vchord work the same way, one at a time)
docker compose up -d postgres
uv run bench --subset full load pg
uv run bench --subset full index trgm   # also: tsv, fts, bm25, textsearch, vchord
uv run bench --subset full runs pg-fts  # top-100 lists -> results/runs/
uv run bench --subset full latency pg-fts --k 10 --concurrency 1 4 16
docker compose stop postgres

# Elasticsearch
docker compose up -d elasticsearch
uv run bench --subset full load es
uv run bench --subset full runs elasticsearch
uv run bench --subset full latency elasticsearch

# Relevance: pool top-20 of all engines, LLM-grade 0/1/2, check a sample by hand
uv run bench --subset full judge
uv run bench calibrate -n 20
uv run bench --subset full eval
```

### Arms

| Arm | Container | Index arms needed | Notes |
|---|---|---|---|
| `pg-ilike` | `postgres` | none | substring match, index scans disabled |
| `pg-trgm` | `postgres` | `trgm` | same SQL, trigram GIN indexes |
| `pg-fts` | `postgres` | `fts` | tsvector + GIN, `ts_rank_cd` |
| `pg-fts-norm` | `postgres` | `fts` | ranking divided by 1 + log(doc length) |
| `pg-search` | `postgres` | `bm25` | ParadeDB BM25 |
| `pg-textsearch` | `pg-textsearch` | `tsv`, `textsearch` | Tiger Data BM25; AND/phrase via the tsv column |
| `pg-textsearch-tuned` | `pg-textsearch` | same | `enable_sort = off` (see `bench/engines.py`) |
| `pg-vchord` | `pg-vchord` | `tsv`, `vchord` | VectorChord-bm25 + pg_tokenizer |
| `elasticsearch` | `elasticsearch` | `load es` | |

Stemming and stopwords are the same everywhere: Postgres `dutch`/`english` snowball configs,
the same stemmer + stopword list in pg_search and pg_tokenizer, Elasticsearch `dutch`/`english` analyzers.
`--subset 10k` uses a fixed 10k-row sample (first 10k by `md5(id)`).

### Latency modes

- default: closed loop, one OS process per client (`--concurrency 1 4 16`).
- `--rate 200`: open loop at a fixed rate; queueing delay counts, so stalls show in p99.
- `--cache`: allow the Elasticsearch shard request cache (off by default, so repeated
  size-0 requests measure engine work). `--zipf 1.0`: skewed query popularity, for cache runs.
- `--facets`: time a city facet count instead of a top-k search.
- Cold runs: restart the engine, drop the OS page cache, run with `--repeats 1 --label cold`.

Results are appended to `results/index.csv`, `results/latency.csv` and `results/relevance.csv`,
tagged with host and timestamp.

## Layout

| Path | What |
|---|---|
| `compose.yaml` | Postgres 17 (ParadeDB, pg_textsearch, VectorChord images), Elasticsearch; all on 127.0.0.1 |
| `docker/` | image build for pg_textsearch |
| `sql/` | table and index arms |
| `bench/engines.py` | one class per search arm |
| `bench/query.py` | query syntax and per-engine rendering |
| `queries/queries.jsonl` | 80 queries (48 website, 32 recruiter), tagged by type |
| `judgments/qrels.jsonl` | graded (query, id) pairs |
