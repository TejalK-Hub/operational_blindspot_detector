-- transform.sql
-- Phase 3 :  Transformation Layer
-- Operational Blindspot Detector
--
-- Aggregates coverage_metrics into a per-domain KPI summary
-- over the most recent 7-day rolling window.
--
-- Output view: domain_kpi_summary
-- Consumed by: coverage_gaps.sql, risk_features.py
--
-- Run order: after ingest.py and validate.py have completed successfully.

-- Drop and recreate the view so this script is safe to re-run.
DROP VIEW IF EXISTS domain_kpi_summary;

CREATE VIEW domain_kpi_summary AS

SELECT
    cm.domain_id,

    -- Rolling window anchor: most recent snapshot date per domain
    MAX(cm.snapshot_date)                           AS latest_snapshot,

    -- Row count gives visibility into how many daily snapshots contributed.
    -- Fewer than 7 may indicate a new domain or a recent data gap.
    COUNT(cm.metric_id)                             AS snapshot_count,

    -- Average null_rate across the 7-day window.
    -- Higher values indicate persistent incompleteness in source data.
    ROUND(AVG(cm.null_rate),   4)                   AS avg_null_rate,

    -- Average stale_rate: proportion of records older than the SLA threshold.
    -- Elevated values point to upstream refresh failures.
    ROUND(AVG(cm.stale_rate),  4)                   AS avg_stale_rate,

    -- Average update lag in hours. Crosses 24h = potential daily pipeline miss.
    ROUND(AVG(cm.update_lag_hrs), 2)                AS avg_update_lag_hrs,

    -- Sum of missing_fields across the window.
    -- A rising total over 7 days signals structural schema drift in the source.
    SUM(cm.missing_fields)                          AS total_missing_fields,

    -- Worst-case values surface peak exposure, not just the average.
    ROUND(MAX(cm.null_rate),   4)                   AS max_null_rate,
    ROUND(MAX(cm.stale_rate),  4)                   AS max_stale_rate,
    ROUND(MAX(cm.update_lag_hrs), 2)                AS max_update_lag_hrs

FROM coverage_metrics cm

WHERE
    -- Restrict to the 7 most recent calendar days relative to the latest
    -- snapshot present in the table. Using MAX() here keeps the query
    -- portable across any snapshot date range in the dataset.
    cm.snapshot_date >= (
        SELECT DATE(MAX(snapshot_date), '-6 days')
        FROM coverage_metrics
    )

GROUP BY
    cm.domain_id

ORDER BY
    avg_null_rate DESC;   -- surface most problematic domains first
