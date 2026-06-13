-- IIC-FORGE F1 schema. Designed upfront per ADR-F4 (revised) so F2/F3/F5
-- additions are append-only (new tables, no column reshapes).
--
-- All TIMESTAMP columns are ISO-8601 strings (TEXT) for SQLite portability.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ============================================================
-- F1 tables — populated from day one
-- ============================================================

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,           -- UUID4 hex
    ticker          TEXT NOT NULL,
    persona_id      TEXT,                       -- nullable for legacy / non-persona runs
    started_ts      TEXT NOT NULL,
    ended_ts        TEXT,
    status          TEXT NOT NULL,              -- "running" | "complete" | "error"
    decision        TEXT,                       -- "BUY" | "HOLD" | "SELL" | NULL
    confidence      REAL,                       -- 0.0–1.0
    trigger_id      TEXT,                       -- nullable; FK to events.event_id when F3 ships
    artifact_dir    TEXT NOT NULL               -- relative path under iic_data_dir
);
CREATE INDEX IF NOT EXISTS idx_runs_ticker_ts ON runs(ticker, started_ts);
CREATE INDEX IF NOT EXISTS idx_runs_persona ON runs(persona_id);

CREATE TABLE IF NOT EXISTS costs (
    cost_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    in_tokens       INTEGER NOT NULL DEFAULT 0,
    out_tokens      INTEGER NOT NULL DEFAULT 0,
    usd_estimate    REAL                        -- nullable; we don't always know the price
);
CREATE INDEX IF NOT EXISTS idx_costs_run ON costs(run_id);

CREATE TABLE IF NOT EXISTS briefs (
    brief_id        TEXT PRIMARY KEY,           -- UUID4 hex
    mode            TEXT NOT NULL,              -- "deep_dive" | "morning_digest" | "event_alert"
    scope           TEXT NOT NULL,              -- single ticker or JSON list
    generated_ts    TEXT NOT NULL,
    content_path    TEXT NOT NULL,              -- relative path under iic_data_dir
    run_ids         TEXT NOT NULL,              -- JSON list of run_id
    delivery_ids    TEXT,                       -- JSON list of delivery_id (F5)
    parent_brief_id TEXT REFERENCES briefs(brief_id)   -- threading for refinement (§4, §10)
);
CREATE INDEX IF NOT EXISTS idx_briefs_parent ON briefs(parent_brief_id);

CREATE TABLE IF NOT EXISTS brief_actions (
    action_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id            TEXT NOT NULL REFERENCES briefs(brief_id) ON DELETE CASCADE,
    action_type         TEXT NOT NULL,          -- "run_backtest" | "refine_brief"
    action_params       TEXT NOT NULL,          -- JSON
    state               TEXT NOT NULL,          -- "pending" | "accepted" | "declined" | "expired"
    expires_at          TEXT NOT NULL,
    responded_at        TEXT,
    result_backtest_id  INTEGER,                -- FK to backtests.backtest_id (F2)
    result_brief_id     TEXT REFERENCES briefs(brief_id)
);
CREATE INDEX IF NOT EXISTS idx_brief_actions_brief ON brief_actions(brief_id);
CREATE INDEX IF NOT EXISTS idx_brief_actions_state ON brief_actions(state, expires_at);

CREATE TABLE IF NOT EXISTS suppression (
    key             TEXT PRIMARY KEY,           -- e.g. "AAPL:macro" or "AAPL:*"
    until_ts        TEXT NOT NULL,
    reason          TEXT,
    created_by      TEXT
);

-- Hybrid memory: per-(persona, component) partitioned
CREATE TABLE IF NOT EXISTS memories (
    memory_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    persona_id      TEXT NOT NULL,              -- NEVER allow "" or "*"; isolation depends on this
    component       TEXT NOT NULL,              -- e.g. "decision_log", future: "bull", "bear", ...
    situation_md    TEXT NOT NULL,
    outcome         TEXT,
    vec_id          INTEGER,                        -- FK to vec_index.rowid; enforced in app layer (virtual tables cannot be FK targets in SQLite)
    created_ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_partition ON memories(persona_id, component);

-- Shared cross-persona outcome pool
CREATE TABLE IF NOT EXISTS outcome_log (
    outcome_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    decision        TEXT NOT NULL,
    outcome_md      TEXT NOT NULL,
    pnl_proxy       REAL,                       -- set by F2 reflection loop
    vec_id          INTEGER,                        -- FK to vec_index.rowid; enforced in app layer (virtual tables cannot be FK targets in SQLite)
    tags            TEXT,                       -- JSON
    created_ts      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_outcome_log_ticker ON outcome_log(ticker);

-- sqlite-vec virtual table (created at runtime by db.py after loading the extension)
-- The placeholder below documents the shape; the actual CREATE VIRTUAL TABLE
-- statement runs after sqlite_vec.load(conn).
-- CREATE VIRTUAL TABLE vec_index USING vec0(embedding float[384]);

-- ============================================================
-- F2 tables — defined upfront, populated when F2 ships
-- ============================================================

CREATE TABLE IF NOT EXISTS backtests (
    backtest_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    triggered_by_brief_id   TEXT REFERENCES briefs(brief_id),   -- set for brief-scoped (F5 flow)
    universe                TEXT NOT NULL,                       -- JSON list of tickers
    start_date              TEXT NOT NULL,
    end_date                TEXT NOT NULL,
    status                  TEXT NOT NULL,
    report_path             TEXT,                                -- relative path under iic_data_dir
    created_ts              TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    btr_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    backtest_id     INTEGER NOT NULL REFERENCES backtests(backtest_id) ON DELETE CASCADE,
    persona_id      TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    metrics         TEXT NOT NULL                                -- JSON: sharpe, total_return, win_rate, ...
);

CREATE TABLE IF NOT EXISTS analysis_packs (
    pack_id        TEXT PRIMARY KEY,
    event_id       TEXT REFERENCES events(event_id),
    ticker         TEXT NOT NULL,
    trade_date     TEXT NOT NULL,
    source_run_ids TEXT NOT NULL,
    content_path   TEXT NOT NULL,
    created_ts     TEXT NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_analysis_packs_event_ticker
    ON analysis_packs(event_id, ticker);

-- ============================================================
-- F3 tables — defined upfront, populated when F3 ships
-- ============================================================

CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL,
    ingested_ts     TEXT NOT NULL,
    salience        REAL,
    raw_path        TEXT,
    deduped_of      TEXT REFERENCES events(event_id),
    status          TEXT NOT NULL                                -- "new" | "triaged" | "discarded" | "duplicate"
);

CREATE TABLE IF NOT EXISTS event_ticker (
    event_id        TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    ticker          TEXT NOT NULL,
    confidence      REAL,
    PRIMARY KEY (event_id, ticker)
);

CREATE TABLE IF NOT EXISTS watchlist (
    ticker          TEXT PRIMARY KEY,
    added_ts        TEXT NOT NULL,
    last_briefed    TEXT,
    ttl_until       TEXT,
    tags            TEXT                                         -- JSON
);

-- ============================================================
-- F4 / F5 tables — defined upfront, populated later
-- ============================================================

CREATE TABLE IF NOT EXISTS queue_jobs (
    job_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    job_type        TEXT NOT NULL,
    payload         TEXT NOT NULL,                               -- JSON
    state           TEXT NOT NULL,                               -- "queued" | "running" | "done" | "error"
    enqueued_ts     TEXT NOT NULL,
    started_ts      TEXT,
    finished_ts     TEXT
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id        TEXT NOT NULL REFERENCES briefs(brief_id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,                               -- "telegram" | "email" | "cli"
    status          TEXT NOT NULL,                               -- "sent" | "failed" | "skipped"
    sent_ts         TEXT
);

-- ============================================================
-- F3 sensing/triage append-only tables (added by IIC-FORGE-06)
-- ============================================================

CREATE TABLE IF NOT EXISTS ingest_cursor (
    source     TEXT PRIMARY KEY,           -- e.g., "polygon_news"
    cursor     TEXT NOT NULL,              -- adapter-specific opaque payload
    updated_ts TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tickers (
    ticker     TEXT PRIMARY KEY,           -- "AAPL", "BTC-USD"
    exchange   TEXT NOT NULL,              -- "NASDAQ" | "NYSE" | "ARCA" | "CRYPTO"
    name       TEXT NOT NULL,
    aliases    TEXT,                       -- JSON array: ["Apple", "Apple Computer"]
    active     INTEGER NOT NULL DEFAULT 1, -- 0 = delisted (filtered)
    updated_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tickers_active ON tickers(active);

CREATE TABLE IF NOT EXISTS event_fingerprints (
    fingerprint TEXT NOT NULL,             -- external_id or sha256 hex
    kind        TEXT NOT NULL,             -- 'external_id' | 'sha256'
    event_id    TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    source      TEXT NOT NULL,
    created_ts  TEXT NOT NULL,
    PRIMARY KEY (fingerprint, kind)
);
CREATE INDEX IF NOT EXISTS idx_event_fingerprints_event ON event_fingerprints(event_id);

CREATE TABLE IF NOT EXISTS event_embeddings (
    event_id   TEXT PRIMARY KEY REFERENCES events(event_id) ON DELETE CASCADE,
    vec_id     INTEGER NOT NULL,           -- app-layer FK to vec_index.rowid
    created_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_embeddings_vec ON event_embeddings(vec_id);

-- ============================================================
-- F4 orchestrator append-only columns (added by IIC-FORGE-07)
-- ============================================================
-- NOTE: ALTER TABLE ADD COLUMN is NOT idempotent in SQLite. The db.py
-- migration layer wraps these statements to swallow "duplicate column
-- name" errors, allowing connect() to be called repeatedly. Do NOT add
-- IF NOT EXISTS — sqlite does not support it on ALTER TABLE.

ALTER TABLE queue_jobs ADD COLUMN trigger_event_id  TEXT REFERENCES events(event_id);
ALTER TABLE queue_jobs ADD COLUMN run_ids           TEXT;
ALTER TABLE queue_jobs ADD COLUMN brief_id          TEXT REFERENCES briefs(brief_id);
ALTER TABLE queue_jobs ADD COLUMN cost_usd          REAL;
ALTER TABLE queue_jobs ADD COLUMN error             TEXT;

ALTER TABLE briefs     ADD COLUMN trigger_event_id  TEXT REFERENCES events(event_id);

ALTER TABLE runs       ADD COLUMN queue_job_id      INTEGER REFERENCES queue_jobs(job_id);
ALTER TABLE briefs     ADD COLUMN analysis_pack_id  TEXT REFERENCES analysis_packs(pack_id);
ALTER TABLE runs       ADD COLUMN analysis_pack_id  TEXT REFERENCES analysis_packs(pack_id);

CREATE INDEX IF NOT EXISTS idx_queue_jobs_trigger_event
    ON queue_jobs(trigger_event_id);
CREATE INDEX IF NOT EXISTS idx_queue_jobs_state_enqueued
    ON queue_jobs(state, enqueued_ts);

CREATE TABLE IF NOT EXISTS alert_evaluations (
    evaluation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id      TEXT NOT NULL REFERENCES events(event_id) ON DELETE CASCADE,
    tickers       TEXT NOT NULL,
    decision      TEXT NOT NULL,
    score         REAL NOT NULL,
    payload       TEXT NOT NULL,
    created_ts    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alert_evaluations_event
    ON alert_evaluations(event_id);

-- ============================================================
-- F5 delivery + operations append-only columns (added by IIC-FORGE-08)
-- ============================================================
ALTER TABLE deliveries ADD COLUMN skip_reason       TEXT;
ALTER TABLE deliveries ADD COLUMN channel_ref       TEXT;
ALTER TABLE briefs     ADD COLUMN refine_depth      INTEGER NOT NULL DEFAULT 0;
ALTER TABLE briefs     ADD COLUMN refine_overrides  TEXT;

ALTER TABLE brief_actions ADD COLUMN result_job_id INTEGER REFERENCES queue_jobs(job_id);
ALTER TABLE brief_actions ADD COLUMN dispatched_ts TEXT;
ALTER TABLE brief_actions ADD COLUMN error TEXT;

CREATE INDEX IF NOT EXISTS idx_deliveries_brief
    ON deliveries(brief_id);
CREATE INDEX IF NOT EXISTS idx_brief_actions_pending_expires
    ON brief_actions(state, expires_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS idx_brief_actions_result_job
    ON brief_actions(result_job_id);

-- ============================================================
-- P0 instrumentation: DeepSeek prompt-cache token capture
-- ============================================================
-- DeepSeek's API reports per-call cache usage (prompt_cache_hit_tokens /
-- prompt_cache_miss_tokens). We persist the per-(run, model) totals next to
-- the existing in/out token counts so a cache hit ratio can be computed from
-- the DB. Both nullable: other providers don't report them, and rows written
-- before this migration keep NULL. Same idempotent-ALTER pattern as above —
-- db.py swallows the "duplicate column name" error on re-run; no IF NOT EXISTS
-- (sqlite does not support it on ALTER TABLE).
ALTER TABLE costs ADD COLUMN cache_hit_tokens  INTEGER;
ALTER TABLE costs ADD COLUMN cache_miss_tokens INTEGER;

-- ============================================================
-- Task 10: evaluator telemetry columns on alert_evaluations
-- ============================================================
-- Same idempotent-ALTER pattern as above — db.py swallows the
-- "duplicate column name" error on re-run; no IF NOT EXISTS
-- (sqlite does not support it on ALTER TABLE).
ALTER TABLE alert_evaluations ADD COLUMN model_id    TEXT;
ALTER TABLE alert_evaluations ADD COLUMN parse_ok    INTEGER;
ALTER TABLE alert_evaluations ADD COLUMN latency_ms  INTEGER;

-- ============================================================
-- Task 13: shadow_eval — per-call rows from the replay harness
-- ============================================================
-- Each row records one event replayed through BOTH the API quick model and a
-- local candidate model.  A row may exercise one role only:
--   * Triage role only  → api_salience/local_salience/salience_delta set;
--                          api_verdict/local_verdict are NULL.
--   * Alert-gate only   → api_verdict/local_verdict set;
--                          salience columns are NULL.
--   * Both roles        → all columns non-NULL.
--
-- Column semantics:
--   shadow_id       — autoincrement surrogate PK; defines insertion order.
--   event_id        — TEXT (NOT NULL): the replayed event.  Intentionally a
--                     plain TEXT column with NO FK to events(event_id) so that
--                     shadow evidence rows are immune to event-lifecycle
--                     operations (deletions or pruning).  Sibling tables such
--                     as event_ticker and event_fingerprints use FK + ON DELETE
--                     CASCADE; we deviate here so that shadow rows survive event
--                     deletion and remain available for the Task-14 reporter.
--   model_id        — TEXT NOT NULL: identifier of the candidate model under
--                     test (e.g. "deepseek-r1-0528").
--   api_salience    — REAL nullable: salience score produced by the API model
--                     for the triage role.  NULL for verdict-only rows.
--   local_salience  — REAL nullable: salience score produced by the local
--                     candidate.  NULL for verdict-only rows.
--   salience_delta  — REAL nullable: local_salience − api_salience, stored
--                     explicitly (derivable but pre-computed by the harness for
--                     direct MAE queries).  NULL for verdict-only rows or when
--                     either salience is NULL.
--   api_verdict     — TEXT nullable: 'pass'/'reject' from the API alert-gate.
--                     NULL for triage-only rows.
--   local_verdict   — TEXT nullable: 'pass'/'reject' from the local
--                     alert-gate.  NULL for triage-only rows.
--   parse_ok        — INTEGER (0/1 NOT NULL): whether the local model's
--                     response was successfully parsed by the harness.  The
--                     harness gates on parse failures; this column enables
--                     per-model parse-failure-rate analysis.
--   latency_ms      — INTEGER nullable: wall-clock latency of the LOCAL model
--                     call in milliseconds.  NULL if not measured.
--   created_ts      — TEXT NOT NULL: UTC ISO-8601 timestamp when this row was
--                     written (same convention as all other created_ts columns
--                     in this schema).
CREATE TABLE IF NOT EXISTS shadow_eval (
    shadow_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id        TEXT    NOT NULL,               -- plain TEXT; no FK (see comment above)
    model_id        TEXT    NOT NULL,               -- candidate model under test
    api_salience    REAL,                           -- API triage score; NULL for verdict-only rows
    local_salience  REAL,                           -- local triage score; NULL for verdict-only rows
    salience_delta  REAL,                           -- local − api; NULL when salience not exercised
    api_verdict     TEXT,                           -- 'pass'/'reject'; NULL for triage-only rows
    local_verdict   TEXT,                           -- 'pass'/'reject'; NULL for triage-only rows
    parse_ok        INTEGER NOT NULL,               -- 1 = local parse succeeded; 0 = failed
    latency_ms      INTEGER,                        -- local model wall-clock latency (ms)
    created_ts      TEXT    NOT NULL                -- UTC ISO-8601 string
);
CREATE INDEX IF NOT EXISTS idx_shadow_eval_model ON shadow_eval(model_id);

-- ============================================================
-- Task 15: availability policy (D5) — deferred salience + ops counters
-- ============================================================
-- events.salience_source records HOW the salience value was produced:
--   'llm'      — scored by a live model call
--   'cache'    — served from the Redis salience cache
--   'deferred' — the scorer could not produce a score (LLM endpoint or parse
--                failure); salience is NULL (un-scored — can never cross the
--                promote threshold, which uses `salience >= ?`) and dedupe
--                fingerprints/embeddings are deliberately NOT recorded so a
--                redelivery of the same payload is RE-SCORED instead of being
--                swallowed as a duplicate.
-- NULL for rows written before this migration and for duplicate rows.
-- Same idempotent-ALTER pattern as above — db.py swallows the "duplicate
-- column name" error on re-run; no IF NOT EXISTS (unsupported on ALTER TABLE).
ALTER TABLE events ADD COLUMN salience_source TEXT;

-- Small persistent ops counters (name → monotonically increasing value),
-- written by the availability layer (AvailabilityCounter / DailyFallbackBudget
-- in tradingagents/llm_clients/availability.py).  Persisted — rather than
-- in-memory only — because the L3 soak gate must query "failure counter = 0"
-- across daemon restarts (an in-memory counter dies with the process).
-- Names in use:
--   'triage_llm_failures'
--       — monotonic, per-EVENT: one bump per envelope whose salience the
--         scorer deferred, INCLUDING parse_error defers (transport fine,
--         model emitted garbage);
--   'promoter_llm_failures'
--       — monotonic, per-CYCLE: one bump per poll cycle skipped on a gate
--         TRANSPORT failure only; a gate parse failure counts NOTHING
--         (neither bump nor consecutive-run reset).
--   UNITS therefore differ between the two daemons (events vs cycles, parse
--   failures counted vs ignored) — deliberate asymmetry, compare with care;
--   full rationale in availability.py's module docstring.
--   'triage_fallback_calls:<YYYY-MM-DD>', 'promoter_fallback_calls:<YYYY-MM-DD>'
--       — per-UTC-day fallback API call counts (hard daily budget enforcement).
CREATE TABLE IF NOT EXISTS ops_counters (
    name       TEXT PRIMARY KEY,
    value      INTEGER NOT NULL DEFAULT 0,
    updated_ts TEXT NOT NULL
);
