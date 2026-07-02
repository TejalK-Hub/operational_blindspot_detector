"""
validate.py
-----------
Phase 2 - Validation Layer
Operational Blindspot Detector

Runs a suite of data quality checks against the coverage_metrics and domains
tables after ingestion. All issues are written to the audit_log table.

Design intent:
  - Each check is an isolated function returning a list of issue strings.
  - Checks are collected, then bulk-written to audit_log in one pass.
  - A WARNING status is non-fatal; a FAILED status halts downstream processing.
  - Run this after ingest.py, before any SQL transforms or ML steps.
"""

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime

# Configuration

DB_PATH = Path("db/blindspot.db")

# Thresholds - centralised here so they're easy to tune via config.yaml later
THRESHOLDS = {
    "null_rate_max":      1.0,   # must be between 0 and 1
    "null_rate_min":      0.0,
    "stale_rate_max":     1.0,
    "stale_rate_min":     0.0,
    "update_lag_min":     0.0,   # hours; negative lag makes no sense
    "missing_fields_min": 0,     # cannot be negative
}

# Required columns for each table
REQUIRED_DOMAINS_COLS  = ["domain_id", "domain_name", "owner_team", "data_source"]
REQUIRED_METRICS_COLS  = [
    "domain_id", "snapshot_date", "total_records",
    "null_rate", "stale_rate", "update_lag_hrs", "missing_fields",
]


# Database helpers

def get_connection() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH.resolve()}. "
            "Run ingest.py first."
        )
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def load_table(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    """Load an entire table into a DataFrame for in-memory validation."""
    return pd.read_sql(f"SELECT * FROM {table}", conn)


# Audit logging

def write_audit_batch(
    conn: sqlite3.Connection,
    issues: list[dict],
) -> None:
    """
    Insert multiple rows into audit_log in one transaction.

    Each item in `issues` is a dict with keys:
        stage, status, rows_affected, notes
    """
    if not issues:
        return

    run_ts = datetime.utcnow().isoformat()
    conn.executemany(
        """
        INSERT INTO audit_log (run_ts, stage, status, rows_affected, notes)
        VALUES (:run_ts, :stage, :status, :rows_affected, :notes)
        """,
        [{**issue, "run_ts": run_ts} for issue in issues],
    )
    conn.commit()


def make_issue(stage: str, status: str, rows: int, notes: str) -> dict:
    """Helper to build a single audit_log issue dict."""
    return {
        "stage":        stage,
        "status":       status,
        "rows_affected": rows,
        "notes":        notes,
    }


# Check 1 -  Required columns

def check_required_columns(
    df: pd.DataFrame, required: list[str], table_name: str
) -> list[dict]:
    """
    Verify that all expected columns exist in the loaded DataFrame.
    A missing column means the schema has drifted or the CSV was altered.
    This is a FAILED condition -  downstream SQL will break without these columns.
    """
    issues = []
    missing = [col for col in required if col not in df.columns]

    if missing:
        issues.append(make_issue(
            stage=f"validate.{table_name}.columns",
            status="FAILED",
            rows=0,
            notes=f"Missing required columns: {missing}",
        ))
        print(f"  [FAILED] {table_name} -  missing columns: {missing}")
    else:
        print(f"  [OK]     {table_name} -  all required columns present.")

    return issues


# Check 2 - Duplicate (domain_id, snapshot_date) combinations

def check_duplicate_keys(df: pd.DataFrame) -> list[dict]:
    """
    Each (domain_id, snapshot_date) pair must be unique in coverage_metrics.
    Duplicates inflate KPI averages and corrupt risk scores downstream.
    This is a WARNING -  the duplicate rows are logged but processing continues.
    """
    issues = []

    dupes = df[df.duplicated(subset=["domain_id", "snapshot_date"], keep=False)]

    if not dupes.empty:
        dupe_count = len(dupes)
        dupe_pairs = (
            dupes[["domain_id", "snapshot_date"]]
            .drop_duplicates()
            .to_dict(orient="records")
        )
        issues.append(make_issue(
            stage="validate.coverage_metrics.duplicate_keys",
            status="WARNING",
            rows=dupe_count,
            notes=f"Duplicate (domain_id, snapshot_date) pairs found: {dupe_pairs}",
        ))
        print(f"  [WARN]   coverage_metrics -  {dupe_count} duplicate key rows found.")
    else:
        print("  [OK]     coverage_metrics -  no duplicate (domain_id, snapshot_date) pairs.")

    return issues


# Check 3 - Null values in critical fields

def check_nulls(df: pd.DataFrame, table_name: str) -> list[dict]:
    """
    Critical numeric and key fields must not be null.
    Nulls in these columns will silently corrupt aggregations.

    WARNING if nulls found -  allows pipeline to continue but flags the domain.
    """
    issues = []

    critical_cols = {
        "coverage_metrics": [
            "domain_id", "snapshot_date", "null_rate",
            "stale_rate", "update_lag_hrs", "missing_fields",
        ],
        "domains": ["domain_id", "domain_name"],
    }.get(table_name, [])

    for col in critical_cols:
        if col not in df.columns:
            continue  # already flagged by check_required_columns
        null_count = df[col].isna().sum()
        if null_count > 0:
            issues.append(make_issue(
                stage=f"validate.{table_name}.nulls.{col}",
                status="WARNING",
                rows=int(null_count),
                notes=f"Column '{col}' has {null_count} null value(s).",
            ))
            print(f"  [WARN]   {table_name}.{col} -  {null_count} null(s) found.")

    if not issues:
        print(f"  [OK]     {table_name} -  no nulls in critical fields.")

    return issues


# Check 4 - Numeric range validation

def check_numeric_ranges(df: pd.DataFrame) -> list[dict]:
    """
    Validate that numeric KPI fields fall within logically valid bounds.

    null_rate and stale_rate must be in [0.0, 1.0] -  they are proportions.
    update_lag_hrs must be >= 0 -  negative time is impossible.
    missing_fields must be >= 0 -  a negative count makes no sense.

    Out-of-range values indicate a broken data source or a unit error
    (e.g. lag expressed in minutes instead of hours). This is a WARNING.
    """
    issues = []

    range_checks = [
        # (column, min_val, max_val, description)
        ("null_rate",       0.0, 1.0,  "proportion [0–1]"),
        ("stale_rate",      0.0, 1.0,  "proportion [0–1]"),
        ("update_lag_hrs",  0.0, None, "must be >= 0"),
        ("missing_fields",  0,   None, "must be >= 0"),
    ]

    for col, min_val, max_val, desc in range_checks:
        if col not in df.columns:
            continue

        col_series = pd.to_numeric(df[col], errors="coerce")

        # Below minimum
        if min_val is not None:
            below = (col_series < min_val).sum()
            if below > 0:
                issues.append(make_issue(
                    stage=f"validate.coverage_metrics.range.{col}",
                    status="WARNING",
                    rows=int(below),
                    notes=f"'{col}' has {below} row(s) below minimum {min_val} ({desc}).",
                ))
                print(f"  [WARN]   coverage_metrics.{col} -  {below} row(s) below min {min_val}.")

        # Above maximum
        if max_val is not None:
            above = (col_series > max_val).sum()
            if above > 0:
                issues.append(make_issue(
                    stage=f"validate.coverage_metrics.range.{col}",
                    status="WARNING",
                    rows=int(above),
                    notes=f"'{col}' has {above} row(s) above maximum {max_val} ({desc}).",
                ))
                print(f"  [WARN]   coverage_metrics.{col} -  {above} row(s) above max {max_val}.")

    if not issues:
        print("  [OK]     coverage_metrics -  all numeric ranges valid.")

    return issues


# Check 5 : Referential integrity: metrics reference valid domain_ids

def check_referential_integrity(
    metrics_df: pd.DataFrame, domains_df: pd.DataFrame
) -> list[dict]:
    """
    Every domain_id in coverage_metrics must exist in the domains table.
    Orphaned metrics produce misleading dashboards and KPI aggregations.
    This is a WARNING -  flagged but not fatal.
    """
    issues = []

    valid_ids   = set(domains_df["domain_id"].dropna())
    metrics_ids = set(metrics_df["domain_id"].dropna())
    orphaned    = metrics_ids - valid_ids

    if orphaned:
        orphaned_rows = metrics_df[metrics_df["domain_id"].isin(orphaned)]
        issues.append(make_issue(
            stage="validate.coverage_metrics.referential_integrity",
            status="WARNING",
            rows=len(orphaned_rows),
            notes=f"Orphaned domain_ids in coverage_metrics (not in domains table): {sorted(orphaned)}",
        ))
        print(f"  [WARN]   coverage_metrics -  {len(orphaned_rows)} rows reference unknown domain_ids: {sorted(orphaned)}")
    else:
        print("  [OK]     coverage_metrics -  all domain_ids resolve to known domains.")

    return issues


# Summary helpers

def summarise_issues(all_issues: list[dict]) -> None:
    """Print a concise summary after all checks complete."""
    if not all_issues:
        print("\n[validate] ✓ All checks passed. No issues found.")
        return

    failures = [i for i in all_issues if i["status"] == "FAILED"]
    warnings = [i for i in all_issues if i["status"] == "WARNING"]

    print(f"\n[validate] Validation summary:")
    print(f"  FAILED  : {len(failures)}")
    print(f"  WARNINGS: {len(warnings)}")

    for issue in all_issues:
        tag = "✗" if issue["status"] == "FAILED" else "⚠"
        print(f"  {tag} [{issue['status']}] {issue['stage']} -  {issue['notes']}")

    if failures:
        print("\n[validate] ✗ One or more FAILED checks -  halting pipeline.")
        raise RuntimeError(
            f"Validation failed with {len(failures)} error(s). "
            "Check audit_log for details."
        )


#---------------------------------------  Main entry point ---------------------------------------

def run() -> None:
    """Run all validation checks and write results to audit_log."""
    print(f"\n{'='*55}")
    print(f"[validate] Starting validation -  {datetime.utcnow().isoformat()}")
    print(f"[validate] Database: {DB_PATH.resolve()}")
    print(f"{'='*55}")

    conn = get_connection()
    all_issues: list[dict] = []

    try:
        domains_df = load_table(conn, "domains")
        metrics_df = load_table(conn, "coverage_metrics")

        print(f"\n[validate] Loaded {len(domains_df)} domain(s), "
              f"{len(metrics_df)} metric row(s).\n")

        # --- domains checks ---
        print("[validate] Checking domains table ...")
        all_issues += check_required_columns(domains_df, REQUIRED_DOMAINS_COLS, "domains")
        all_issues += check_nulls(domains_df, "domains")

        # --- coverage_metrics checks ---
        print("\n[validate] Checking coverage_metrics table ...")
        all_issues += check_required_columns(metrics_df, REQUIRED_METRICS_COLS, "coverage_metrics")
        all_issues += check_duplicate_keys(metrics_df)
        all_issues += check_nulls(metrics_df, "coverage_metrics")
        all_issues += check_numeric_ranges(metrics_df)
        all_issues += check_referential_integrity(metrics_df, domains_df)

        # Write all issues (warnings + failures) to audit_log in one batch
        if all_issues:
            write_audit_batch(conn, all_issues)
            print(f"\n[validate] {len(all_issues)} issue(s) written to audit_log.")
        else:
            # Record a clean-pass entry so every run is traceable
            write_audit_batch(conn, [make_issue(
                stage="validate",
                status="SUCCESS",
                rows=len(metrics_df),
                notes="All validation checks passed.",
            )])

        # Raises RuntimeError if any FAILED checks - must be last
        summarise_issues(all_issues)

    except RuntimeError:
        # Re-raise validation failures so run_pipeline.py can catch them
        raise

    except Exception as e:
        print(f"\n[validate] ✗ Unexpected error during validation: {e}")
        write_audit_batch(conn, [make_issue(
            stage="validate",
            status="FAILED",
            rows=0,
            notes=f"Unexpected exception: {e}",
        )])
        raise

    finally:
        conn.close()
        print("[validate] Connection closed.\n")


if __name__ == "__main__":
    run()
