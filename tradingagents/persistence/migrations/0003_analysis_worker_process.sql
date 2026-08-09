-- IIC-FORGE migration 0003: process-isolated analysis worker lifecycle.

-- The parent worker owns queue state while one killable child process performs
-- analysis. These fields preserve the last process identity/outcome and make
-- permanent blocks distinguishable from retry exhaustion.
ALTER TABLE queue_jobs ADD COLUMN error_category TEXT;
ALTER TABLE queue_jobs ADD COLUMN operator_note TEXT;
ALTER TABLE queue_jobs ADD COLUMN worker_pid INTEGER
    CHECK (worker_pid IS NULL OR worker_pid > 0);
ALTER TABLE queue_jobs ADD COLUMN last_exit_code INTEGER;
ALTER TABLE queue_jobs ADD COLUMN blocked_ts TEXT;

CREATE INDEX idx_queue_jobs_blocked
    ON queue_jobs(state, blocked_ts, job_id)
    WHERE state = 'blocked';
