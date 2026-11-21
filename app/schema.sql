CREATE TABLE IF NOT EXISTS publications (
    book TEXT PRIMARY KEY,
    payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    book TEXT NOT NULL REFERENCES publications(book),
    payload TEXT NOT NULL,
    sequence INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS run_inputs (
    run_id TEXT NOT NULL REFERENCES runs(id),
    source TEXT NOT NULL,
    data BLOB NOT NULL,
    PRIMARY KEY (run_id, source)
);
CREATE INDEX IF NOT EXISTS run_head ON runs(book, sequence DESC);
CREATE TABLE IF NOT EXISTS chargeback_notices (
    notice_id TEXT PRIMARY KEY,
    parent_notice_id TEXT REFERENCES chargeback_notices(notice_id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    book TEXT NOT NULL,
    ticket TEXT NOT NULL,
    channel TEXT NOT NULL,
    policy TEXT NOT NULL,
    requested_cents INTEGER NOT NULL,
    status TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    evidence_due_at TEXT NOT NULL,
    note TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chargeback_run ON chargeback_notices(run_id, registered_at);
CREATE TABLE IF NOT EXISTS chargeback_reversals (
    reversal_id TEXT PRIMARY KEY,
    notice_id TEXT NOT NULL REFERENCES chargeback_notices(notice_id),
    run_id TEXT NOT NULL,
    reversed_at TEXT NOT NULL,
    registered_at TEXT NOT NULL,
    note TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS chargeback_reversal_notice ON chargeback_reversals(notice_id);
CREATE TABLE IF NOT EXISTS chargeback_snapshots (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    policy TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    payload TEXT NOT NULL
);
