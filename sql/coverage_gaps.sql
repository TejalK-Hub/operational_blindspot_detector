-- coverage_gaps.sql
-- Phase 3 - Transformation Layer
-- Operational Blindspot Detector
--
-- Flags domains that breach at least one blindspot threshold.
-- Joins domain_kpi_summary (from transform.sql) with the domains table
-- to enrich results with domain_name and owner_team for reporting.
--
-- Threshold logic (OR conditions -  any one breach = candidate blindspot):
--   avg_update_lag_hrs > 24   -->  likely missing a daily pipeline run
--   avg_null_rate      > 0.15 --> more than 15% of fields are empty
--   avg_stale_rate     > 0.20 --> more than 20% of records past SLA age
--
-- Output view : coverage_gap_candidates
-- Consumed by : risk_features.py, Power BI alerts page
--
-- Prerequisite: transform.sql must have been applied first (creates
--               the domain_kpi_summary view that this query depends on).

DROP VIEW IF EXISTS coverage_gap_candidates;

CREATE VIEW coverage_gap_candidates AS

SELECT
    d.domain_id,
    d.domain_name,
    d.owner_team,
    d.data_source,

    -- KPI aggregates from the rolling 7 - day window
    kpi.latest_snapshot,
    kpi.snapshot_count,
    kpi.avg_null_rate,
    kpi.avg_stale_rate,
    kpi.avg_update_lag_hrs,
    kpi.total_missing_fields,

    -- Peak exposure columns help triage: a domain with a high max but low
    -- average may have had a one-off incident rather than a chronic problem.
    kpi.max_null_rate,
    kpi.max_stale_rate,
    kpi.max_update_lag_hrs,

    -- Human-readable flags for each individual threshold breach.
    -- These drive the "top driver" column in risk_scorer.py (Phase 4).
    CASE WHEN kpi.avg_update_lag_hrs > 24   THEN 1 ELSE 0 END AS flag_stale_lag,
    CASE WHEN kpi.avg_null_rate      > 0.15 THEN 1 ELSE 0 END AS flag_high_nulls,
    CASE WHEN kpi.avg_stale_rate     > 0.20 THEN 1 ELSE 0 END AS flag_high_stale,

    -- Count of breached thresholds (0 - 3).
    -- 3 = domain is critically blind across all three dimensions.
    (
        CASE WHEN kpi.avg_update_lag_hrs > 24   THEN 1 ELSE 0 END +
        CASE WHEN kpi.avg_null_rate      > 0.15 THEN 1 ELSE 0 END +
        CASE WHEN kpi.avg_stale_rate     > 0.20 THEN 1 ELSE 0 END
    )                                                           AS breach_count,

    -- Derived label for Power BI slicer and dashboard colouring.
    -- Maps breach_count to a human-readable severity tier.
    CASE
        WHEN (
            CASE WHEN kpi.avg_update_lag_hrs > 24   THEN 1 ELSE 0 END +
            CASE WHEN kpi.avg_null_rate      > 0.15 THEN 1 ELSE 0 END +
            CASE WHEN kpi.avg_stale_rate     > 0.20 THEN 1 ELSE 0 END
        ) = 3 THEN 'CRITICAL'
        WHEN (
            CASE WHEN kpi.avg_update_lag_hrs > 24   THEN 1 ELSE 0 END +
            CASE WHEN kpi.avg_null_rate      > 0.15 THEN 1 ELSE 0 END +
            CASE WHEN kpi.avg_stale_rate     > 0.20 THEN 1 ELSE 0 END
        ) = 2 THEN 'HIGH'
        ELSE 'MEDIUM'
    END                                                         AS gap_severity

FROM domain_kpi_summary kpi

-- Inner join: only domains that exist in the domains master table
-- are included. Orphaned metrics are surfaced by validate.py instead.
INNER JOIN domains d
    ON kpi.domain_id = d.domain_id

WHERE
    -- Keep only domains that breach at least one threshold.
    -- Remove this WHERE clause to return all domains (useful for full
    -- visibility reports where healthy domains should also appear).
    kpi.avg_update_lag_hrs > 24
    OR kpi.avg_null_rate   > 0.15
    OR kpi.avg_stale_rate  > 0.20

ORDER BY
    breach_count DESC,          -- most-breached domains first
    avg_update_lag_hrs DESC;    -- secondary sort: longest lag at top
