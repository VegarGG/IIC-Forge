-- IIC-FORGE migration 0002: durable work and alert-delivery queues.

-- Analysis-job queue: idempotent enqueue, bounded attempts, scheduled retry,
-- and fenced leases. Existing rows remain valid and immediately eligible.
ALTER TABLE queue_jobs ADD COLUMN idempotency_key TEXT;
ALTER TABLE queue_jobs ADD COLUMN attempt_count INTEGER NOT NULL DEFAULT 0
    CHECK (attempt_count >= 0);
ALTER TABLE queue_jobs ADD COLUMN max_attempts INTEGER NOT NULL DEFAULT 3
    CHECK (max_attempts > 0);
ALTER TABLE queue_jobs ADD COLUMN available_ts TEXT;
ALTER TABLE queue_jobs ADD COLUMN lease_token TEXT;
ALTER TABLE queue_jobs ADD COLUMN lease_expires_ts TEXT;
ALTER TABLE queue_jobs ADD COLUMN last_error_ts TEXT;

UPDATE queue_jobs
SET available_ts = enqueued_ts
WHERE available_ts IS NULL;

CREATE UNIQUE INDEX uq_queue_jobs_idempotency_key
    ON queue_jobs(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX idx_queue_jobs_ready
    ON queue_jobs(state, available_ts, job_id);
CREATE INDEX idx_queue_jobs_lease_expiry
    ON queue_jobs(state, lease_expires_ts)
    WHERE state = 'running';

-- Delivery outbox: every event alert is persisted before a transport attempt.
-- One row is mutated through its lifecycle; deliveries remains the immutable
-- per-attempt audit trail and stores provider references/errors.
CREATE TABLE delivery_queue (
    delivery_job_id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id        TEXT NOT NULL REFERENCES briefs(brief_id) ON DELETE CASCADE,
    channel         TEXT NOT NULL,
    mode            TEXT NOT NULL,
    brief_payload   TEXT NOT NULL,
    body            TEXT NOT NULL,
    state           TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN ('queued', 'running', 'sent', 'dead')),
    attempt_count   INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    max_attempts    INTEGER NOT NULL DEFAULT 5 CHECK (max_attempts > 0),
    available_ts    TEXT NOT NULL,
    lease_token     TEXT,
    lease_expires_ts TEXT,
    last_error      TEXT,
    last_error_ts   TEXT,
    last_delivery_id INTEGER REFERENCES deliveries(delivery_id),
    created_ts      TEXT NOT NULL,
    updated_ts      TEXT NOT NULL,
    UNIQUE (brief_id, channel)
);

CREATE INDEX idx_delivery_queue_ready
    ON delivery_queue(state, available_ts, delivery_job_id);
CREATE INDEX idx_delivery_queue_lease_expiry
    ON delivery_queue(state, lease_expires_ts)
    WHERE state = 'running';
