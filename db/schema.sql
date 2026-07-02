-- Operational domains being monitored
CREATE TABLE IF NOT EXISTS domains (
    domain_id     TEXT PRIMARY KEY,
    domain_name   TEXT NOT NULL,         -- e.g. "Supply Chain", "Finance"
    owner_team    TEXT,
    data_source   TEXT
);

-- One row per domain per day: coverage health metrics
CREATE TABLE IF NOT EXISTS coverage_metrics (
    metric_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id        TEXT REFERENCES domains(domain_id),
    snapshot_date    DATE NOT NULL,
    total_records    INTEGER,
    null_rate        REAL,               -- 0.0 – 1.0
    stale_rate       REAL,               -- % records older than threshold
    update_lag_hrs   REAL,               -- avg hours since last update
    missing_fields   INTEGER,            -- count of expected fields absent
    created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Scored output: one row per domain per run
CREATE TABLE IF NOT EXISTS risk_scores (
    score_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    domain_id        TEXT REFERENCES domains(domain_id),
    run_date         DATE NOT NULL,
    composite_score  REAL,               -- 0 (healthy) – 100 (blind)
    cluster_label    INTEGER,            -- KMeans cluster id
    risk_tier        TEXT,               -- LOW / MEDIUM / HIGH / CRITICAL
    top_driver       TEXT                -- which KPI drove the score
);

-- Audit trail for every pipeline run
CREATE TABLE IF NOT EXISTS audit_log (
    run_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_ts       TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    stage        TEXT,
    status       TEXT,                   -- SUCCESS / FAILED / WARNING
    rows_affected INTEGER,
    notes        TEXT
);