"""
risk_features.py
----------------
Phase 3 - Feature Engineering Layer
Operational Blindspot Detector

Loads the aggregated KPI view (domain_kpi_summary) from SQLite, builds a
clean feature matrix suitable for scikit-learn clustering, applies
StandardScaler, and exports two artefacts to data/processed/:

  risk_features.csv       - scaled features, one row per domain, with
                            domain_id retained as an index column so
                            cluster_model.py and risk_scorer.py can join
                            results back to domain metadata.

  risk_features_raw.csv   - unscaled features, useful for audit and for
                            Power BI KPI drill-down (raw values are more
                            readable than z-scores in a dashboard).

Feature matrix columns (4 features, matching architecture spec):
  avg_null_rate
  avg_stale_rate
  avg_update_lag
  total_missing_fields

Run order: after ingest.py, validate.py, and after transform.sql has been
           applied to the database (creates the domain_kpi_summary view).
"""

import sqlite3
import pandas as pd
from pathlib import Path
from sklearn.preprocessing import StandardScaler
from datetime import datetime

# Configuration

DB_PATH       = Path("db/blindspot.db")
PROCESSED_DIR = Path("data/processed")

OUTPUT_SCALED = PROCESSED_DIR / "risk_features.csv"
OUTPUT_RAW    = PROCESSED_DIR / "risk_features_raw.csv"

# Exact column names used in the feature matrix - it  must match domain_kpi_summary
FEATURE_COLS = [
    "avg_null_rate",
    "avg_stale_rate",
    "avg_update_lag_hrs",   # aliased to avg_update_lag in the output
    "total_missing_fields",
]

# Rename map: keeps output column names clean and consistent with spec
FEATURE_RENAME = {
    "avg_update_lag_hrs": "avg_update_lag",
}


# Database helpers

def get_connection() -> sqlite3.Connection:
    if not DB_PATH.exists():
        raise FileNotFoundError(
            f"Database not found at {DB_PATH.resolve()}. "
            "Run ingest.py first."
        )
    return sqlite3.connect(DB_PATH)


def apply_sql_file(conn: sqlite3.Connection, sql_path: Path) -> None:
    """
    Execute a .sql file against the open connection.
    Used to ensure the views from transform.sql are present before querying.
    Safe to re-run -  both views use DROP IF EXISTS before CREATE.
    """
    print(f"[risk_features] Applying {sql_path.name} ...")
    sql = sql_path.read_text()
    conn.executescript(sql)
    print(f"[risk_features] {sql_path.name} applied.")


# Data loading

def load_kpi_summary(conn: sqlite3.Connection) -> pd.DataFrame:
    """
    Load the domain_kpi_summary view into a DataFrame.

    Selects domain_id alongside the four feature columns so the domain
    identifier travels with the data through scaling and export.
    """
    query = """
        SELECT
            domain_id,
            avg_null_rate,
            avg_stale_rate,
            avg_update_lag_hrs,
            total_missing_fields
        FROM domain_kpi_summary
        ORDER BY domain_id
    """
    df = pd.read_sql(query, conn)
    print(f"[risk_features] Loaded {len(df)} domain(s) from domain_kpi_summary.")
    return df


# Feature engineering

def validate_features(df: pd.DataFrame) -> None:
    """
    Lightweight pre-scaling sanity check.
    Raises ValueError if the DataFrame is empty or any feature column is
    entirely null -  either condition would produce meaningless cluster output.
    """
    if df.empty:
        raise ValueError(
            "domain_kpi_summary returned no rows. "
            "Ensure transform.sql has been applied and coverage_metrics is populated."
        )

    for col in FEATURE_COLS:
        if col not in df.columns:
            raise ValueError(
                f"Expected feature column '{col}' not found in domain_kpi_summary. "
                "Check transform.sql output."
            )
        if df[col].isna().all():
            raise ValueError(
                f"Feature column '{col}' is entirely null -  cannot scale."
            )

    # Warn (don't fail) on partial nulls - impute with column median below
    for col in FEATURE_COLS:
        null_count = df[col].isna().sum()
        if null_count > 0:
            print(f"  [WARN] '{col}' has {null_count} null(s) -  will impute with column median.")


def build_feature_matrix(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Isolate the four feature columns, impute any remaining nulls with the
    column median (robust to outliers), and return:
      - feature_df  : raw (unscaled) feature DataFrame, domain_id as a column
      - features    : pure numeric matrix ready for StandardScaler
    """
    feature_df = df[["domain_id"] + FEATURE_COLS].copy()

    # Impute nulls with median -  median is less sensitive to outlier domains
    # (e.g. a single critical domain skewing the mean)
    for col in FEATURE_COLS:
        if feature_df[col].isna().any():
            median_val = feature_df[col].median()
            feature_df[col] = feature_df[col].fillna(median_val)
            print(f"  [info]  Imputed nulls in '{col}' with median={median_val:.4f}.")

    # Apply the rename map before export (raw CSV uses clean names too)
    feature_df = feature_df.rename(columns=FEATURE_RENAME)

    return feature_df


def scale_features(feature_df: pd.DataFrame) -> pd.DataFrame:
    """
    Apply StandardScaler to the four numeric feature columns.

    StandardScaler transforms each feature to zero mean and unit variance.
    This prevents update_lag_hrs (which can be in the tens or hundreds)
    from dominating distance calculations in KMeans clustering.

    Returns a new DataFrame with:
      - domain_id (unscaled -  preserved as join key)
      - scaled feature columns (suffixed with _scaled for clarity)
    """
    numeric_cols = [c for c in feature_df.columns if c != "domain_id"]

    scaler    = StandardScaler()
    scaled_np = scaler.fit_transform(feature_df[numeric_cols])

    scaled_df = pd.DataFrame(
        scaled_np,
        columns=[f"{c}_scaled" for c in numeric_cols],
        index=feature_df.index,
    )

    # Print scaling parameters for reproducibility / debugging
    print("\n[risk_features] StandardScaler parameters:")
    for col, mean, std in zip(numeric_cols, scaler.mean_, scaler.scale_):
        print(f"  {col:<30} mean={mean:.4f}  std={std:.4f}")

    # Combine domain_id with scaled features in one export-ready DataFrame
    result = pd.concat([feature_df[["domain_id"]].reset_index(drop=True),
                        scaled_df.reset_index(drop=True)], axis=1)

    return result


# Export

def export(df: pd.DataFrame, path: Path, label: str) -> None:
    """Write a DataFrame to CSV, creating the output directory if needed."""
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False)
    print(f"[risk_features] {label} exported → {path.resolve()}  ({len(df)} rows)")


#------------------------------------  Main entry point ------------------------------------

def run() -> None:
    print(f"\n{'='*55}")
    print(f"[risk_features] Starting feature engineering -  {datetime.utcnow().isoformat()}")
    print(f"[risk_features] Database: {DB_PATH.resolve()}")
    print(f"{'='*55}\n")

    conn = get_connection()

    try:
        # Ensure the SQL views are present before querying them.
        # These are idempotent (DROP IF EXISTS + CREATE VIEW).
        sql_dir = Path("sql")
        apply_sql_file(conn, sql_dir / "transform.sql")
        apply_sql_file(conn, sql_dir / "coverage_gaps.sql")

        # Load aggregated KPI data
        raw_df = load_kpi_summary(conn)

        # Pre-scaling validation
        print("\n[risk_features] Validating feature columns ...")
        validate_features(raw_df)
        print("  [OK]  All feature columns present and non-empty.")

        # Build raw feature matrix (with renaming applied)
        print("\n[risk_features] Building feature matrix ...")
        feature_df = build_feature_matrix(raw_df)
        print(f"  Feature shape: {len(feature_df)} rows × {len(feature_df.columns)-1} features")

        # Scale features
        print("\n[risk_features] Scaling features with StandardScaler ...")
        scaled_df = scale_features(feature_df)

        # Export both artefacts
        print("\n[risk_features] Exporting ...")
        export(scaled_df,  OUTPUT_SCALED, "Scaled features  (risk_features.csv)")
        export(feature_df, OUTPUT_RAW,    "Raw features     (risk_features_raw.csv)")

        # Preview
        print("\n[risk_features] Scaled feature matrix preview:")
        print(scaled_df.to_string(index=False))

        print(f"\n[risk_features] ✓ Feature engineering complete.\n")

    except Exception as e:
        print(f"\n[risk_features] ✗ Feature engineering failed: {e}")
        raise

    finally:
        conn.close()
        print("[risk_features] Connection closed.")


if __name__ == "__main__":
    run()
