-- IIC-FORGE migration 0006: Batch 9 operator observability and controls.

-- Process liveness is explicit.  A heartbeat is deliberately separate from
-- queue activity: an idle worker can be healthy and a recently finished job
-- does not prove that its worker still exists.
CREATE TABLE service_heartbeats (
    service_name    TEXT PRIMARY KEY,
    instance_id     TEXT NOT NULL,
    process_id      INTEGER,
    started_ts      TEXT NOT NULL,
    heartbeat_ts    TEXT NOT NULL,
    status          TEXT NOT NULL
                    CHECK (status IN ('active', 'degraded', 'stopping')),
    success_ts      TEXT,
    failure_ts      TEXT,
    failure_count   INTEGER NOT NULL DEFAULT 0 CHECK (failure_count >= 0),
    detail          TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_service_heartbeats_status
    ON service_heartbeats(status, heartbeat_ts);

-- Alert state and delivery intent are both durable.  The unique key collapses
-- repeated observations of the same fault while occurrence_count preserves
-- the evidence that it continued to happen.
CREATE TABLE operational_alerts (
    alert_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    dedup_key         TEXT NOT NULL UNIQUE,
    category          TEXT NOT NULL,
    severity          TEXT NOT NULL
                      CHECK (severity IN ('warning', 'critical')),
    summary           TEXT NOT NULL,
    details           TEXT NOT NULL DEFAULT '{}',
    state             TEXT NOT NULL DEFAULT 'open'
                      CHECK (state IN ('open', 'resolved')),
    first_seen_ts     TEXT NOT NULL,
    last_seen_ts      TEXT NOT NULL,
    occurrence_count  INTEGER NOT NULL DEFAULT 1 CHECK (occurrence_count > 0),
    last_delivery_ts  TEXT,
    delivery_brief_id TEXT REFERENCES briefs(brief_id),
    resolved_ts       TEXT,
    operator_note     TEXT
);
CREATE INDEX idx_operational_alerts_state
    ON operational_alerts(state, severity, last_seen_ts);

-- Append-only human action log for controls that do not already have a queue
-- lifecycle event table of their own.
CREATE TABLE operator_actions (
    action_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type     TEXT NOT NULL,
    target_type     TEXT NOT NULL,
    target_id       TEXT NOT NULL,
    requested_ts    TEXT NOT NULL,
    operator_note   TEXT NOT NULL,
    result          TEXT NOT NULL,
    metadata        TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_operator_actions_requested
    ON operator_actions(requested_ts, action_id);

-- A reservation release never edits the original LLM ledger.  The one-to-one
-- foreign key makes the adjustment auditable and prevents double release.
CREATE TABLE llm_budget_releases (
    call_id          TEXT PRIMARY KEY REFERENCES llm_budget_ledger(call_id),
    released_usd     REAL NOT NULL CHECK (released_usd > 0),
    released_ts      TEXT NOT NULL,
    operator_note    TEXT NOT NULL,
    evidence         TEXT NOT NULL
);

-- Restore drills are explicit operator evidence; merely verifying an archive
-- is not represented as a successful restore.
CREATE TABLE recovery_drills (
    drill_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_name      TEXT NOT NULL,
    archive_sha256    TEXT NOT NULL CHECK (length(archive_sha256) = 64),
    started_ts        TEXT NOT NULL,
    finished_ts       TEXT NOT NULL,
    elapsed_seconds   REAL NOT NULL CHECK (elapsed_seconds >= 0),
    status            TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
    operator_note     TEXT NOT NULL
);
CREATE INDEX idx_recovery_drills_finished
    ON recovery_drills(finished_ts, drill_id);
