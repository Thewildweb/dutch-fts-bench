# Postgres 17 + Tiger Data's pg_textsearch (BM25), same Postgres minor as the ParadeDB image.
FROM postgres:17.11-trixie
ARG PG_TEXTSEARCH_VERSION=1.5.1
ARG TARGETARCH
RUN apt-get update && apt-get install -y --no-install-recommends curl unzip ca-certificates \
 && curl -fsSL -o /tmp/p.zip "https://github.com/timescale/pg_textsearch/releases/download/v${PG_TEXTSEARCH_VERSION}/pg-textsearch-v${PG_TEXTSEARCH_VERSION}-pg17-${TARGETARCH}.zip" \
 && cd /tmp && unzip -q p.zip && dpkg -i ./pg-textsearch-postgresql-17_*.deb \
 && rm -rf /tmp/p.zip /tmp/*.deb /var/lib/apt/lists/*
