"""
risk_scorer.py
--------------
Phase 4 - Risk Scoring Layer
Operational Blindspot Detector

Loads the clustered feature data, computes a deterministic composite_score
(0 to 100) for each domain, identifies the top_driver (the single KPI that
contributed most to the score), and writes the final risk_scores table to
data/exports/risk_scores.csv -  which is the direct input for Power BI.

Scoring model
-------------
The composite score is a weighted sum of four normalised KPI components.
Each component is capped at 1.0 before weighting so a single extreme domain
cannot push scores above 100.

  Weights (must sum to 1.0):
    avg_null_rate           → 0.30   (30 pts max)
    avg_stale_rate          → 0.25   (25 pts max)
    avg_update_lag          → 0.30   (30 pts max, normalised against 72 hrs)
    total_missing_fields    → 0.15   (15 pts max, normalised against 10 fields)

  composite_score = Σ(component_score * weight) * 100

  Score bands → risk_tier:
    0  - 25   LOW
    26 -  50   MEDIUM
    51 -  75   HIGH
    76 -  100  CRITICAL

Run order: after cluster_model.py has produced clustered_risk_features.csv.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from datetime import datetime, date

# Configuration

PROCESSED_DIR    = Path("data/processed")
EXPORTS_DIR      = Path("data/exports")

INPUT_CLUSTERED  = PROCESSED_DIR / "clustered_risk_features.csv"
OUTPUT_SCORES    = EXPORTS_DIR   / "risk_scores.csv"

# ---- Scoring weights (must sum to 1.0) ------------------------------------
WEIGHTS = {
    "avg_null_rate":         0.30,
    "avg_stale_rate":        0.25,
    "avg_update_lag":        0.30,
    "total_missing_fields":  0.15,
}

# ---- Normalisation ceilings -----------------------------------------------
# Each raw KPI is divided by its ceiling before weighting.
# Values above the ceiling are capped at 1.0 (score contribution maxes out).
# Ceilings represent "as bad as it realistically gets" in enterprise ops.
CEILINGS = {
    "avg_null_rate":         1.0,    # already a proportion [0 - 1]
    "avg_stale_rate":        1.0,    # already a proportion [0 - 1]
    "avg_update_lag":        72.0,   # 72 hrs = 3-day lag -->  max exposure
    "total_missing_fields":  10.0,   # 10 missing fields --> structurally broken
}

# ---- Risk tier bands -------------------------------------------------------
TIER_BANDS = [
    (76, 100, "CRITICAL"),
    (51,  75, "HIGH"),
    (26,  50, "MEDIUM"),
    (0,   25, "LOW"),
]


# Data loading

def load_clustered() -> pd.DataFrame:
    if not INPUT_CLUSTERED.exists():
        raise FileNotFoundError(
            f"Clustered features not found at {INPUT_CLUSTERED.resolve()}. "
            "Run cluster_model.py first."
        )
    df = pd.read_csv(INPUT_CLUSTERED)
    print(f"[scorer] Loaded {len(df)} domain(s) from {INPUT_CLUSTERED.name}.")
    return df


# Scoring logic

def normalise_component(series: pd.Series, ceiling: float) -> pd.Series:
    """
    Divide raw values by their ceiling and cap at 1.0.
    Result is a dimensionless score component in [0.0, 1.0].
    """
    return (series / ceiling).clip(upper=1.0)


def compute_components(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute a normalised score component (0–1) for each of the four KPIs.
    Component columns are added directly to the DataFrame for transparency
    -  they appear in the exported CSV so analysts can audit the math.
    """
    for col, ceiling in CEILINGS.items():
        component_col = f"score_{col}"
        df[component_col] = normalise_component(df[col], ceiling)

    return df


def compute_composite_score(df: pd.DataFrame) -> pd.Series:
    """
    Weighted sum of the four normalised components, scaled to 0–100.

    composite_score = (
        score_avg_null_rate        * 0.30 +
        score_avg_stale_rate       * 0.25 +
        score_avg_update_lag       * 0.30 +
        score_total_missing_fields * 0.15
    ) * 100

    Rounded to 1 decimal place for readability.
    """
    composite = sum(
        df[f"score_{col}"] * weight
        for col, weight in WEIGHTS.items()
    )
    return (composite * 100).round(1)


def assign_risk_tier(score: float) -> str:
    """Map a composite_score to a risk tier label using the defined bands."""
    for low, high, tier in TIER_BANDS:
        if low <= score <= high:
            return tier
    return "LOW"   # fallback for score = 0


def identify_top_driver(row: pd.Series) -> str:
    """
    Return the name of the KPI that contributed the most to this domain's
    composite score (weight × normalised component).

    This is the 'top_driver' column in the risk_scores table -  it gives
    analysts and domain owners an immediate, actionable explanation of what
    is driving the risk signal rather than leaving them to interpret raw scores.
    """
    contributions = {
        col: row[f"score_{col}"] * weight
        for col, weight in WEIGHTS.items()
    }
    return max(contributions, key=contributions.get)


# Tier override from clustering

def reconcile_tier(row: pd.Series) -> str:
    """
    The composite_score produces a score-band tier; the cluster model produces
    a centroid-distance tier. Where they disagree by more than one band, defer
    to the higher (more conservative) of the two.

    This prevents a domain with a borderline score from being under-reported
    if the cluster model placed it firmly in a worse group.

    Score-band tier takes precedence when they agree or differ by one step.
    Cluster tier takes precedence when it is strictly higher.
    """
    tier_rank = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3}

    score_tier_rank   = tier_rank.get(row["risk_tier_score"], 0)
    cluster_tier_rank = tier_rank.get(row["risk_tier_cluster"], 0)

    if cluster_tier_rank > score_tier_rank + 1:
        return row["risk_tier_cluster"]
    return row["risk_tier_score"]


# Summary reporting

def print_score_summary(result_df: pd.DataFrame) -> None:
    """Print a ranked table of all domains with their scores and drivers."""
    print("\n[scorer] Risk score results (ranked highest → lowest):\n")
    print(f"  {'Domain':<7}  {'Score':>6}  {'Tier':<10}  {'Top Driver'}")
    print(f"  {'-'*58}")

    for _, row in result_df.sort_values("composite_score", ascending=False).iterrows():
        print(
            f"  {row['domain_id']:<7}  "
            f"{row['composite_score']:>6.1f}  "
            f"{row['risk_tier']:<10}  "
            f"{row['top_driver']}"
        )

    print(f"\n[scorer] Tier distribution:")
    counts = result_df["risk_tier"].value_counts().reindex(
        ["CRITICAL", "HIGH", "MEDIUM", "LOW"], fill_value=0
    )
    for tier, count in counts.items():
        bar = "█" * count
        print(f"  {tier:<10} {bar}  ({count})")


# Export

def build_export_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    Shape the final DataFrame to match the risk_scores schema exactly:
        domain_id, run_date, composite_score, cluster_label,
        risk_tier, top_driver
    Drops intermediate component and scaled columns -  Power BI only needs
    the final scores plus the raw KPIs (which arrive via coverage_metrics.csv).
    """
    export_cols = [
        "domain_id",
        "run_date",
        "composite_score",
        "cluster_label",
        "risk_tier",
        "top_driver",
    ]
    return df[export_cols].copy()


def export(df: pd.DataFrame) -> None:
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_SCORES, index=False)
    print(f"\n[scorer] Exported → {OUTPUT_SCORES.resolve()}  ({len(df)} rows)")


# -------------------------------- Main entry point --------------------------------

def run() -> pd.DataFrame:
    """
    Full scoring pipeline.
    Returns the final DataFrame so run_pipeline.py can use it downstream.
    """
    print(f"\n{'='*55}")
    print(f"[scorer] Starting risk scoring -  {datetime.utcnow().isoformat()}")
    print(f"{'='*55}\n")

    df = load_clustered()

    # Verify required columns exist before scoring
    required = list(WEIGHTS.keys()) + ["domain_id", "cluster_label", "risk_tier"]
    missing  = [c for c in required if c not in df.columns]
    if missing:
        raise ValueError(
            f"Required columns missing from {INPUT_CLUSTERED.name}: {missing}. "
            "Re-run cluster_model.py."
        )

    # Rename risk_tier from clustering to disambiguate during reconciliation
    df = df.rename(columns={"risk_tier": "risk_tier_cluster"})

    # Step 1: normalise each KPI into a 0 - 1 component score
    df = compute_components(df)

    # Step 2: weighted sum -->  composite_score (0 - 100)
    df["composite_score"] = compute_composite_score(df)

    # Step 3: score-band tier from composite_score alone
    df["risk_tier_score"] = df["composite_score"].apply(assign_risk_tier)

    # Step 4: reconcile score-band tier with cluster tier (conservative merge)
    df["risk_tier"] = df.apply(reconcile_tier, axis=1)

    # Step 5: identify the single biggest contributor to each domain's score
    df["top_driver"] = df.apply(identify_top_driver, axis=1)

    # Step 6: stamp the run date (useful for historical trending in Power BI)
    df["run_date"] = date.today().isoformat()

    # Console summary
    print_score_summary(df)

    # Export clean output matching risk_scores schema
    export_df = build_export_df(df)
    export(export_df)

    print(f"\n[scorer] ✓ Risk scoring complete.\n")
    return export_df


if __name__ == "__main__":
    run()
