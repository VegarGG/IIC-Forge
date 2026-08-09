-- IIC-FORGE migration 0004: production delivery-outbox controls.

-- SQLite cannot widen a CHECK constraint in place, so rebuild the mutable
-- outbox lifecycle table. The immutable deliveries table remains untouched.
ALTER TABLE delivery_queue RENAME TO delivery_queue_v3;

CREATE TABLE delivery_queue (
    delivery_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    brief_id        TEXT NOT NULL REFERENCES briefs(brief_id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,
    mode            TEXT NOT NULL,
    brief_payload   TEXT NOT NULL,
    body            TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN (
                        'queued', 'running', 'sent', 'dead', 'blocked', 'cancelled'
                    )),
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts    INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    available_ts    TEXT NOT NULL,
    lease_token     TEXT,
    lease_expires_ts TEXT,
    last_error      TEXT,
    last_error_ts   TEXT,
    error_category  TEXT,
    blocked_ts      TEXT,
    cancelled_ts    TEXT,
    operator_note   TEXT,
    last_delivery_id INTEGER REFERENCES deliveries(delivery_id),
    created_ts      TEXT NOT NULL,
    updated_ts      TEXT NOT NULL,
    UNIQUE (brief_id, channel)
);

INSERT INTO delivery_queue (
    delivery_job_id, idempotency_key, brief_id, channel, mode, brief_payload,
    body, state, attempt_count, max_attempts, available_ts, lease_token,
    lease_expires_ts, last_error, last_error_ts, last_delivery_id, created_ts,
    updated_ts
)
SELECT
    delivery_job_id, 'brief:' || brief_id || ':channel:' || channel,
    brief_id, channel, mode, brief_payload, body, state, attempt_count,
    max_attempts, available_ts, lease_token, lease_expires_ts, last_error,
    last_error_ts, last_delivery_id, created_ts, updated_ts
FROM delivery_queue_v3;

DROP TABLE delivery_queue_v3;

CREATE INDEX idx_delivery_queue_ready
    ON delivery_queue(state, available_ts, delivery_job_id);
CREATE INDEX idx_delivery_queue_lease_expiry
    ON delivery_queue(state, lease_expires_ts)
    WHERE state = 'running';
CREATE INDEX idx_delivery_queue_blocked
    ON delivery_queue(state, blocked_ts, delivery_job_id)
    WHERE state = 'blocked';

-- Operator actions and worker lifecycle transitions are append-only here.
-- The queue row keeps the current state; this table explains how it got there.
CREATE TABLE delivery_queue_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    delivery_job_id INTEGER NOT NULL
                    REFERENCES delivery_queue(delivery_job_id) ON DELETE CASCADE,
    event_type      TEXT NOT NULL,
    from_state      TEXT,
    to_state        TEXT NOT NULL,
    error_category  TEXT,
    note            TEXT,
    created_ts      TEXT NOT NULL
);

CREATE INDEX idx_delivery_queue_events_job
    ON delivery_queue_events(delivery_job_id, event_id);
