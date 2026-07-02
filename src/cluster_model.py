"""
cluster_model.py
----------------
Phase 4 - Clustering Layer
Operational Blindspot Detector

Loads the scaled feature matrix from risk_features.csv, fits a KMeans model
with k=4, maps each cluster to a human-readable risk tier (LOW / MEDIUM /
HIGH / CRITICAL), and exports the labelled result to clustered_risk_features.csv.

The tier assignment is based on each cluster's centroid distance from the
"worst-case" corner of the feature space -  clusters whose centroids sit
furthest from healthy values receive the highest tier labels.

Run order: after risk_features.py has produced data/processed/risk_features.csv.
"""

import pandas as pd
import numpy as np
from pathlib import Path
from sklearn.cluster import KMeans
from datetime import datetime

# Configuration

PROCESSED_DIR   = Path("data/processed")
INPUT_SCALED    = PROCESSED_DIR / "risk_features.csv"
INPUT_RAW       = PROCESSED_DIR / "risk_features_raw.csv"
OUTPUT_CLUSTERED = PROCESSED_DIR / "clustered_risk_features.csv"

# KMeans settings
N_CLUSTERS  = 4
RANDOM_STATE = 42   # fixed seed -->  deterministic output on every run

# Scaled feature columns produced by risk_features.py
SCALED_COLS = [
    "avg_null_rate_scaled",
    "avg_stale_rate_scaled",
    "avg_update_lag_scaled",
    "total_missing_fields_scaled",
]

# Tier labels ordered from safest to most critical.
# Assigned after ranking clusters by centroid severity (check assign_tiers).
TIER_LABELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]


# Data loading

def load_features() -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load both the scaled (for clustering) and raw (for export context)
    feature files produced by risk_features.py.
    """
    if not INPUT_SCALED.exists():
        raise FileNotFoundError(
            f"Scaled features not found at {INPUT_SCALED.resolve()}. "
            "Run risk_features.py first."
        )
    if not INPUT_RAW.exists():
        raise FileNotFoundError(
            f"Raw features not found at {INPUT_RAW.resolve()}. "
            "Run risk_features.py first."
        )

    scaled_df = pd.read_csv(INPUT_SCALED)
    raw_df    = pd.read_csv(INPUT_RAW)

    print(f"[cluster] Loaded {len(scaled_df)} domain(s) from {INPUT_SCALED.name}.")
    return scaled_df, raw_df


# Clustering

def fit_kmeans(scaled_df: pd.DataFrame) -> tuple[KMeans, np.ndarray]:
    """
    Fit KMeans with k=4 on the four scaled feature columns.

    n_init=20 runs the algorithm from 20 different random initialisations
    and picks the best result (lowest inertia). Combined with RANDOM_STATE,
    this produces deterministic, stable cluster assignments across runs.
    """
    X = scaled_df[SCALED_COLS].values

    model = KMeans(
        n_clusters=N_CLUSTERS,
        n_init=20,              # more inits --> more stable centroids
        random_state=RANDOM_STATE,
    )
    labels = model.fit_predict(X)

    print(f"[cluster] KMeans fitted. Inertia: {model.inertia_:.4f}")
    return model, labels


def assign_tiers(model: KMeans) -> dict[int, str]:
    """
    Map raw cluster IDs (0–3) to risk tier labels (LOW / MEDIUM / HIGH / CRITICAL).

    Strategy: rank clusters by their average centroid value across all four
    scaled features. A higher average means the cluster sits further in the
    "bad" direction of the scaled space (higher nulls, lag, staleness, etc.),
    so it receives a higher severity tier.

    This approach is fully deterministic and does not rely on the arbitrary
    integer labels KMeans assigns (which can shift between runs if centroids
    converge differently).
    """
    # Average centroid value per cluster across all feature dimensions
    centroid_severity = model.cluster_centers_.mean(axis=1)

    # argsort ascending --> index 0 = least severe cluster, index 3 = most severe
    ranked_clusters = np.argsort(centroid_severity)

    tier_map = {
        int(cluster_id): TIER_LABELS[rank]
        for rank, cluster_id in enumerate(ranked_clusters)
    }

    print("\n[cluster] Cluster → tier mapping:")
    for cluster_id, tier in sorted(tier_map.items()):
        severity = centroid_severity[cluster_id]
        print(f"  Cluster {cluster_id} → {tier:<8}  (avg centroid score: {severity:+.4f})")

    return tier_map


# Summary reporting

def print_cluster_summary(result_df: pd.DataFrame) -> None:
    """
    Print a per-tier summary: how many domains, and the average raw KPI values.
    This gives an immediate sense of what each tier "looks like" in practice.
    """
    print("\n[cluster] Cluster summary (raw KPI averages per tier):")
    print(f"  {'Tier':<10} {'Domains':>7}  {'Null%':>7}  {'Stale%':>7}  {'Lag(h)':>8}  {'MissFields':>11}")
    print(f"  {'-'*60}")

    for tier in TIER_LABELS:
        subset = result_df[result_df["risk_tier"] == tier]
        if subset.empty:
            continue
        print(
            f"  {tier:<10} {len(subset):>7}  "
            f"{subset['avg_null_rate'].mean():>6.1%}  "
            f"{subset['avg_stale_rate'].mean():>6.1%}  "
            f"{subset['avg_update_lag'].mean():>8.1f}  "
            f"{subset['total_missing_fields'].mean():>11.1f}"
        )

    print(f"\n[cluster] Domains per tier:")
    tier_counts = result_df["risk_tier"].value_counts().reindex(TIER_LABELS, fill_value=0)
    for tier, count in tier_counts.items():
        bar = "█" * count
        print(f"  {tier:<10} {bar}  ({count})")


# Export

def export(df: pd.DataFrame) -> None:
    OUTPUT_CLUSTERED.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUTPUT_CLUSTERED, index=False)
    print(f"\n[cluster] Exported → {OUTPUT_CLUSTERED.resolve()}  ({len(df)} rows)")


# --------------------- Main entry point ---------------------

def run() -> pd.DataFrame:
    """
    Full clustering pipeline.
    Returns the final DataFrame so run_pipeline.py can pass it directly
    to risk_scorer.py without a second disk read.
    """
    print(f"\n{'='*55}")
    print(f"[cluster] Starting clustering -  {datetime.utcnow().isoformat()}")
    print(f"{'='*55}\n")

    scaled_df, raw_df = load_features()

    # Validate that scaled feature columns are present
    missing_cols = [c for c in SCALED_COLS if c not in scaled_df.columns]
    if missing_cols:
        raise ValueError(
            f"Scaled feature columns missing from {INPUT_SCALED.name}: {missing_cols}. "
            "Re-run risk_features.py."
        )

    # Fit model and get raw cluster labels
    model, raw_labels = fit_kmeans(scaled_df)

    # Translate raw cluster IDs to semantic tier labels
    tier_map = assign_tiers(model)
    tier_labels = [tier_map[lbl] for lbl in raw_labels]

    # Build output: raw features + cluster metadata
    result_df = raw_df.copy()
    result_df["cluster_label"] = raw_labels
    result_df["risk_tier"]     = tier_labels

    # Summary to console
    print_cluster_summary(result_df)

    # Export
    export(result_df)

    print(f"\n[cluster] ✓ Clustering complete.\n")
    return result_df


if __name__ == "__main__":
    run()
