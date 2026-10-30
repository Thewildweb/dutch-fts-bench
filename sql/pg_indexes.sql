-- Index arms. Each block is built and timed separately by `bench index pg <arm>`.
-- {table} is jobs or jobs_10k. Per-document analyzer: nl -> dutch, en -> english, other -> simple.

-- @arm trgm
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS {table}_title_trgm ON {table} USING gin (title gin_trgm_ops);
CREATE INDEX IF NOT EXISTS {table}_text_trgm ON {table} USING gin (job_text gin_trgm_ops);

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
    ((CASE WHEN lang = 'nl' THEN title END)::pdb.simple('stemmer=dutch', 'alias=title_nl')),
    ((CASE WHEN lang = 'en' THEN title END)::pdb.simple('stemmer=english', 'alias=title_en')),
    ((CASE WHEN lang NOT IN ('nl', 'en') THEN title END)::pdb.simple('alias=title_xx')),
    ((CASE WHEN lang = 'nl' THEN job_text END)::pdb.simple('stemmer=dutch', 'alias=text_nl')),
    ((CASE WHEN lang = 'en' THEN job_text END)::pdb.simple('stemmer=english', 'alias=text_en')),
    ((CASE WHEN lang NOT IN ('nl', 'en') THEN job_text END)::pdb.simple('alias=text_xx')),
    lang, city, last_seen
);
