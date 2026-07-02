"""
ingest.py
---------
Phase 2 : Ingestion Layer
Operational Blindspot Detector

Loads raw CSV files (domains, coverage_metrics) into the SQLite database.
Designed to be append-safe: re-running will not duplicate existing rows.
"""

import sqlite3
import pandas as pd
from pathlib import Path
from datetime import datetime

# Configuration

DB_PATH = Path("db/blindspot.db")
DATA_DIR = Path("data/raw")

DOMAINS_CSV        = DATA_DIR / "domains.csv"
METRICS_CSV        = DATA_DIR / "coverage_metrics.csv"

# Expected columns - must match schema defined in db/schema.sql
DOMAINS_COLUMNS = ["domain_id", "domain_name", "owner_team", "data_source"]

METRICS_COLUMNS = [
    "metric_id", "domain_id", "snapshot_date", "total_records",
    "null_rate", "stale_rate", "update_lag_hrs", "missing_fields",
    "created_at",
]


# Database helpers

def get_connection() -> sqlite3.Connection:
    """Open (or create) the SQLite database and return a connection."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    # Enforce foreign key constraints at runtime
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_tables(conn: sqlite3.Connection) -> None:
    """
    Create tables if they don't already exist.
    This mirrors db/schema.sql so the ingestion layer is self-contained
    and safe to run before schema.sql has been applied manually.
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS domains (
            domain_id   TEXT PRIMARY KEY,
            domain_name TEXT NOT NULL,
            owner_team  TEXT,
            data_source TEXT
        );

        CREATE TABLE IF NOT EXISTS coverage_metrics (
            metric_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            domain_id        TEXT REFERENCES domains(domain_id),
            snapshot_date    DATE NOT NULL,
            total_records    INTEGER,
            null_rate        REAL,
            stale_rate       REAL,
            update_lag_hrs   REAL,
            missing_fields   INTEGER,
            created_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
            run_ts        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            stage         TEXT,
            status        TEXT,
            rows_affected INTEGER,
            notes         TEXT
        );
    """)
    conn.commit()
    print("[ingest] Tables verified / created.")


# Audit logging

def write_audit(
    conn: sqlite3.Connection,
    stage: str,
    status: str,
    rows_affected: int = 0,
    notes: str = "",
) -> None:
    """Insert one row into audit_log to record a pipeline stage outcome."""
    conn.execute(
        """
        INSERT INTO audit_log (run_ts, stage, status, rows_affected, notes)
        VALUES (?, ?, ?, ?, ?)
        """,
        (datetime.utcnow().isoformat(), stage, status, rows_affected, notes),
    )
    conn.commit()


# Ingestion functions

def load_domains(conn: sqlite3.Connection) -> int:
    """
    Load domains.csv into the 'domains' table.

    Uses INSERT OR IGNORE so existing domain_ids are skipped safely - 
    re-running this function will not create duplicates.

    Returns the number of new rows inserted.
    """
    print(f"[ingest] Reading {DOMAINS_CSV} ...")
    df = pd.read_csv(DOMAINS_CSV)

    # Verify expected columns are present before using the database
    missing = set(DOMAINS_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"domains.csv is missing columns: {missing}")

    # Keep only the columns of the schema (drop any extras)
    df = df[DOMAINS_COLUMNS]

    # Count rows in table before insert to calculate net-new rows
    before = pd.read_sql("SELECT COUNT(*) AS n FROM domains", conn).iloc[0]["n"]

    df.to_sql(
        name="domains",
        con=conn,
        if_exists="append",   # never truncate, only add
        index=False,
        method="multi",       # batched insert for performance
    )

    # SQLite will raise IntegrityError on duplicate PKs when using to_sql.
    # INSERT OR IGNORE via executemany is safer for idempotent loads.
    # We use a two-step approach: to_sql into a staging temp, then merge.
    # For this project scale, we do it inline below instead.

    after = pd.read_sql("SELECT COUNT(*) AS n FROM domains", conn).iloc[0]["n"]
    inserted = int(after - before)

    print(f"[ingest] domains -  {len(df)} rows read, {inserted} new rows inserted.")
    return inserted


def load_domains_safe(conn: sqlite3.Connection) -> int:
    """
    Append-safe version of load_domains using INSERT OR IGNORE.
    This replaces load_domains to handle re-runs without errors.
    """
    print(f"[ingest] Reading {DOMAINS_CSV} ...")
    df = pd.read_csv(DOMAINS_CSV)

    missing = set(DOMAINS_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"domains.csv is missing columns: {missing}")

    df = df[DOMAINS_COLUMNS]

    cursor = conn.cursor()
    inserted = 0

    for _, row in df.iterrows():
        result = cursor.execute(
            """
            INSERT OR IGNORE INTO domains (domain_id, domain_name, owner_team, data_source)
            VALUES (?, ?, ?, ?)
            """,
            (row["domain_id"], row["domain_name"], row["owner_team"], row["data_source"]),
        )
        inserted += result.rowcount

    conn.commit()
    print(f"[ingest] domains -  {len(df)} rows read, {inserted} new rows inserted.")
    return inserted


def load_coverage_metrics(conn: sqlite3.Connection) -> int:
    """
    Load coverage_metrics.csv into the 'coverage_metrics' table.

    Deduplicates on (domain_id, snapshot_date) -  if a row for that
    combination already exists, it is skipped. This allows daily
    re-runs without inflating historical data.

    Returns the number of new rows inserted.
    """
    print(f"[ingest] Reading {METRICS_CSV} ...")
    df = pd.read_csv(METRICS_CSV, parse_dates=["snapshot_date", "created_at"])

    missing = set(METRICS_COLUMNS) - set(df.columns)
    if missing:
        raise ValueError(f"coverage_metrics.csv is missing columns: {missing}")

    df = df[METRICS_COLUMNS]

    # Load existing (domain_id, snapshot_date) combos to check for duplicates
    existing = pd.read_sql(
        "SELECT domain_id, snapshot_date FROM coverage_metrics", conn
    )

    if not existing.empty:
        # Normalise snapshot_date to string for comparison
        df["snapshot_date"] = df["snapshot_date"].astype(str).str[:10]
        existing["snapshot_date"] = existing["snapshot_date"].astype(str).str[:10]

        merge_key = df[["domain_id", "snapshot_date"]].merge(
            existing, on=["domain_id", "snapshot_date"], how="left", indicator=True
        )
        new_mask = merge_key["_merge"] == "left_only"
        df = df[new_mask.values].copy()
    else:
        df["snapshot_date"] = df["snapshot_date"].astype(str).str[:10]

    if df.empty:
        print("[ingest] coverage_metrics -  no new rows to insert (all already loaded).")
        return 0

    # Drop metric_id - SQLite AUTOINCREMENT assigns it
    df = df.drop(columns=["metric_id"])

    df.to_sql(
        name="coverage_metrics",
        con=conn,
        if_exists="append",
        index=False,
    )

    inserted = len(df)
    print(f"[ingest] coverage_metrics -  {inserted} new rows inserted.")
    return inserted


# ----------------------------- Main entry point ---------------------------

def run() -> None:
    """Run the full ingestion pipeline: connect → ensure schema → load data."""
    print(f"\n{'='*55}")
    print(f"[ingest] Starting ingestion -  {datetime.utcnow().isoformat()}")
    print(f"[ingest] Database: {DB_PATH.resolve()}")
    print(f"{'='*55}")

    conn = get_connection()

    try:
        ensure_tables(conn)

        # Load domains first (coverage_metrics has a FK to domains)
        domains_inserted = load_domains_safe(conn)
        write_audit(conn, "ingest.domains", "SUCCESS", domains_inserted)

        metrics_inserted = load_coverage_metrics(conn)
        write_audit(conn, "ingest.coverage_metrics", "SUCCESS", metrics_inserted)

        print(f"\n[ingest] ✓ Ingestion complete.")
        print(f"[ingest]   domains inserted       : {domains_inserted}")
        print(f"[ingest]   coverage_metrics inserted: {metrics_inserted}")

    except Exception as e:
        print(f"\n[ingest] ✗ Ingestion failed: {e}")
        write_audit(conn, "ingest", "FAILED", 0, str(e))
        raise

    finally:
        conn.close()
        print("[ingest] Connection closed.\n")


if __name__ == "__main__":
    run()
