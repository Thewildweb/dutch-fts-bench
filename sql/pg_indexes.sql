-- Index arms. Each block is built and timed separately by `bench index pg <arm>`.
-- {table} is jobs or jobs_10k. Per-document analyzer: nl -> dutch, en -> english, other -> simple.

-- @arm trgm
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS {table}_title_trgm ON {table} USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS {table}_text_trgm ON {table} USING gin (job_text gin_trgm_ops);

-- @arm tsv
-- Stored tsvector column only (pg_textsearch uses it for AND/phrase/exclude checks).
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector(CASE lang WHEN 'nl' THEN 'dutch'::regconfig
                                    WHEN 'en' THEN 'english'::regconfig
                                    ELSE 'simple'::regconfig END, title), 'A') ||
    setweight(to_tsvector(CASE lang WHEN 'nl' THEN 'dutch'::regconfig
                                    WHEN 'en' THEN 'english'::regconfig
                                    ELSE 'simple'::regconfig END, job_text), 'B')
) STORED;

-- @arm fts
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tsv tsvector GENERATED ALWAYS AS (
    setweight(to_tsvector(CASE lang WHEN 'nl' THEN 'dutch'::regconfig
                                    WHEN 'en' THEN 'english'::regconfig
                                    ELSE 'simple'::regconfig END, title), 'A') ||
    setweight(to_tsvector(CASE lang WHEN 'nl' THEN 'dutch'::regconfig
                                    WHEN 'en' THEN 'english'::regconfig
                                    ELSE 'simple'::regconfig END, job_text), 'B')
) STORED;
CREATE INDEX IF NOT EXISTS {table}_tsv ON {table} USING gin (tsv);

-- @arm bm25
CREATE EXTENSION IF NOT EXISTS pg_search;
CREATE INDEX IF NOT EXISTS {table}_bm25 ON {table} USING bm25 (
    id,
    ((CASE WHEN lang = 'nl' THEN title END)::pdb.simple('stemmer=dutch', 'stopwords_language=dutch', 'alias=title_nl')),
    ((CASE WHEN lang = 'en' THEN title END)::pdb.simple('stemmer=english', 'stopwords_language=english', 'alias=title_en')),
    ((CASE WHEN lang NOT IN ('nl', 'en') THEN title END)::pdb.simple('alias=title_xx')),
    ((CASE WHEN lang = 'nl' THEN job_text END)::pdb.simple('stemmer=dutch', 'stopwords_language=dutch', 'alias=text_nl')),
    ((CASE WHEN lang = 'en' THEN job_text END)::pdb.simple('stemmer=english', 'stopwords_language=english', 'alias=text_en')),
    ((CASE WHEN lang NOT IN ('nl', 'en') THEN job_text END)::pdb.simple('alias=text_xx')),
    -- literal = untokenized and stored column-wise ("fast"), so city facets can be
    -- counted from the index; as plain text they made pg_search read every matching row.
    (lang::pdb.literal), (city::pdb.literal), last_seen
);

-- @arm textsearch
-- Tiger Data pg_textsearch; needs the tsv arm. One index per language config, each on its own
-- expression (NULL for other languages). Partial indexes on one shared expression confused the
-- planner, which then picked the wrong language's index.
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE INDEX IF NOT EXISTS {table}_bm25_nl ON {table} USING bm25
    ((CASE WHEN lang = 'nl' THEN title || E'\n' || job_text END)) WITH (text_config = 'dutch');
CREATE INDEX IF NOT EXISTS {table}_bm25_en ON {table} USING bm25
    ((CASE WHEN lang = 'en' THEN title || E'\n' || job_text END)) WITH (text_config = 'english');
CREATE INDEX IF NOT EXISTS {table}_bm25_xx ON {table} USING bm25
    ((CASE WHEN lang NOT IN ('nl', 'en') THEN title || E'\n' || job_text END)) WITH (text_config = 'simple');

-- @arm textsearch_title
-- Same as textsearch, with the title repeated: a workaround for pg_textsearch having no
-- field weights. Needs the tsv arm.
CREATE EXTENSION IF NOT EXISTS pg_textsearch;
CREATE INDEX IF NOT EXISTS {table}_bm25t_nl ON {table} USING bm25
    ((CASE WHEN lang = 'nl' THEN title || E'\n' || title || E'\n' || job_text END)) WITH (text_config = 'dutch');
CREATE INDEX IF NOT EXISTS {table}_bm25t_en ON {table} USING bm25
    ((CASE WHEN lang = 'en' THEN title || E'\n' || title || E'\n' || job_text END)) WITH (text_config = 'english');
CREATE INDEX IF NOT EXISTS {table}_bm25t_xx ON {table} USING bm25
    ((CASE WHEN lang NOT IN ('nl', 'en') THEN title || E'\n' || title || E'\n' || job_text END)) WITH (text_config = 'simple');


-- @arm bm25_nomutable
-- pg_search tuning for live inserts: write new rows straight into immutable segments.
-- With the default mutable segment, ~400 inserts made top-10 queries ~9x slower
-- (p50 9.5 -> 84 ms) until the index was cleaned up. Revert with RESET (mutable_segment_rows).
ALTER INDEX {table}_bm25 SET (mutable_segment_rows = 0);
