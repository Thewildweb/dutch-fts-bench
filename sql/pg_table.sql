-- {table} is replaced by jobs (full) or jobs_10k (subset).
CREATE TABLE IF NOT EXISTS {table} (
    id          text PRIMARY KEY,
    url         text NOT NULL,
    domain      text NOT NULL,
    title       text NOT NULL,
    lang        text NOT NULL,
    job_text    text NOT NULL,
    text_length integer NOT NULL,
    word_count  integer NOT NULL,
    city        text NOT NULL,
    first_seen  date NOT NULL,
    last_seen   date NOT NULL
);
CREATE INDEX IF NOT EXISTS {table}_lang_idx ON {table} (lang);
CREATE INDEX IF NOT EXISTS {table}_city_idx ON {table} (city);
CREATE INDEX IF NOT EXISTS {table}_last_seen_idx ON {table} (last_seen);
