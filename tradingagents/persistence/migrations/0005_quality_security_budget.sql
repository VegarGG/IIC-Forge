-- IIC-FORGE migration 0005: Batch 7 quality quarantine and global LLM budget.

-- Invalid or policy-violating ingestion is retained as metadata, but never
-- enters the events table and can therefore never reach embedding, an LLM, or
-- the alert promoter.  The raw_path is nullable because oversized payloads
-- and malicious paths are deliberately not copied into canonical storage.
CREATE TABLE ingest_quarantine (
    quarantine_id  TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    external_id    TEXT,
    observed_ts    TEXT NOT NULL,
    reason_codes   TEXT NOT NULL,
    warning_codes  TEXT NOT NULL DEFAULT '[]',
    raw_path       TEXT,
    envelope_sha256 TEXT NOT NULL,
    byte_count     INTEGER,
    details        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_ingest_quarantine_observed
    ON ingest_quarantine(observed_ts, quarantine_id);
CREATE INDEX idx_ingest_quarantine_source
    ON ingest_quarantine(source, observed_ts);

-- One row per paid LLM request.  A reservation is written under BEGIN
-- IMMEDIATE before the network request starts, so every Compose service sees
-- the same Beijing-day balance.  Unknown/error outcomes retain their full
-- reservation as the charged amount; successful calls reconcile to usage.
CREATE TABLE llm_budget_ledger (
    call_id          TEXT PRIMARY KEY,
    budget_date      TEXT NOT NULL,
    budget_timezone  TEXT NOT NULL,
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    state            TEXT NOT NULL
                     CHECK (state IN ('reserved', 'settled', 'charged')),
    reserved_usd     REAL NOT NULL CHECK (reserved_usd >= 0),
    actual_usd       REAL CHECK (actual_usd >= 0),
    prompt_tokens    INTEGER CHECK (prompt_tokens >= 0),
    completion_tokens INTEGER CHECK (completion_tokens >= 0),
    cache_hit_tokens INTEGER CHECK (cache_hit_tokens >= 0),
    cache_miss_tokens INTEGER CHECK (cache_miss_tokens >= 0),
    created_ts       TEXT NOT NULL,
    settled_ts       TEXT,
    error             TEXT
);
CREATE INDEX idx_llm_budget_ledger_day
    ON llm_budget_ledger(budget_date, state, call_id);
