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
    lang, city, last_seen
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

-- @arm vchord
-- VectorChord-bm25 + pg_tokenizer; needs the tsv arm. pg_tokenizer uses Postgres's own snowball
-- dictionaries (dutch_stem / english_stem / simple), so tokens match the tsvector arms. Each
-- language gets its own vocabulary, trained from a generated text column; a trigger keeps the
-- token column current on insert/update.
CREATE EXTENSION IF NOT EXISTS pg_tokenizer CASCADE;
CREATE EXTENSION IF NOT EXISTS vchord_bm25 CASCADE;
SELECT tokenizer_catalog.create_text_analyzer('analyzer_nl', $$
pre_tokenizer = "unicode_segmentation"
[[character_filters]]
to_lowercase = {}
[[token_filters]]
skip_non_alphanumeric = {}
[[token_filters]]
pg_dict = "dutch_stem"
$$);
SELECT tokenizer_catalog.create_text_analyzer('analyzer_en', $$
pre_tokenizer = "unicode_segmentation"
[[character_filters]]
to_lowercase = {}
[[token_filters]]
skip_non_alphanumeric = {}
[[token_filters]]
pg_dict = "english_stem"
$$);
SELECT tokenizer_catalog.create_text_analyzer('analyzer_xx', $$
pre_tokenizer = "unicode_segmentation"
[[character_filters]]
to_lowercase = {}
[[token_filters]]
skip_non_alphanumeric = {}
$$);
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS doc_nl text GENERATED ALWAYS AS
    (CASE WHEN lang = 'nl' THEN title || E'\n' || job_text END) STORED;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS doc_en text GENERATED ALWAYS AS
    (CASE WHEN lang = 'en' THEN title || E'\n' || job_text END) STORED;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS doc_xx text GENERATED ALWAYS AS
    (CASE WHEN lang NOT IN ('nl', 'en') THEN title || E'\n' || job_text END) STORED;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS emb_nl bm25_catalog.bm25vector;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS emb_en bm25_catalog.bm25vector;
ALTER TABLE {table} ADD COLUMN IF NOT EXISTS emb_xx bm25_catalog.bm25vector;
SELECT tokenizer_catalog.create_custom_model_tokenizer_and_trigger(tokenizer_name => '{table}_tok_nl',
    model_name => '{table}_model_nl', text_analyzer_name => 'analyzer_nl',
    table_name => '{table}', source_column => 'doc_nl', target_column => 'emb_nl');
SELECT tokenizer_catalog.create_custom_model_tokenizer_and_trigger(tokenizer_name => '{table}_tok_en',
    model_name => '{table}_model_en', text_analyzer_name => 'analyzer_en',
    table_name => '{table}', source_column => 'doc_en', target_column => 'emb_en');
SELECT tokenizer_catalog.create_custom_model_tokenizer_and_trigger(tokenizer_name => '{table}_tok_xx',
    model_name => '{table}_model_xx', text_analyzer_name => 'analyzer_xx',
    table_name => '{table}', source_column => 'doc_xx', target_column => 'emb_xx');
CREATE INDEX IF NOT EXISTS {table}_vc_nl ON {table} USING bm25 (emb_nl bm25_catalog.bm25_ops);
CREATE INDEX IF NOT EXISTS {table}_vc_en ON {table} USING bm25 (emb_en bm25_catalog.bm25_ops);
CREATE INDEX IF NOT EXISTS {table}_vc_xx ON {table} USING bm25 (emb_xx bm25_catalog.bm25_ops);
