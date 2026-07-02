"""
run_pipeline.py
---------------
Phase 5 : Pipeline Orchestrator
Operational Blindspot Detector

Runs all pipeline stages in sequence:
  1. ingest        -->  load CSVs into SQLite
  2. validate      -->  data quality checks  audit_log
  3. risk_features -->  KPI aggregation + StandardScaler -->  data/processed/
  4. cluster_model -->  KMeans k=4 -->  clustered_risk_features.csv
  5. risk_scorer   -->  composite score + tier + top_driver -->  data/exports/

Halts immediately if validate raises a RuntimeError (FAILED check).
All other stages propagate their own exceptions with a clear stage label.

Usage:
    python run_pipeline.py
"""

import sys
import time
from datetime import datetime
from pathlib import Path

# Add src/ to path so stage modules import cleanly regardless of working dir
sys.path.insert(0, str(Path(__file__).parent / "src"))

import ingest
import validate
import risk_features
import cluster_model
import risk_scorer


# Console helpers

DIVIDER = "=" * 60

def log_stage_start(name: str, index: int, total: int) -> float:
    """Print a stage header and return the start timestamp."""
    print(f"\n{DIVIDER}")
    print(f"  STAGE {index}/{total} - {name}")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(DIVIDER)
    return time.time()


def log_stage_end(name: str, elapsed: float) -> None:
    print(f"\n  DONE :  {name} completed in {elapsed:.2f}s")


def log_pipeline_start() -> float:
    print(f"\n{'#'*60}")
    print(f"  Operational Blindspot Detector -  Pipeline Run")
    print(f"  {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC")
    print(f"{'#'*60}")
    return time.time()


def log_pipeline_end(total_elapsed: float, success: bool) -> None:
    status = "✓ PIPELINE COMPLETE" if success else "✗ PIPELINE HALTED"
    print(f"\n{'#'*60}")
    print(f"  {status}")
    print(f"  Total elapsed: {total_elapsed:.2f}s")
    print(f"{'#'*60}\n")


# Stage runner

def run_stage(name: str, fn, index: int, total: int) -> None:
    """
    Execute a single pipeline stage function with timing and error handling.
    Any exception is re-raised after printing a clear failure message - 
    the caller (run_pipeline) decides whether to halt or continue.
    """
    t0 = log_stage_start(name, index, total)
    try:
        fn()
        log_stage_end(name, time.time() - t0)
    except RuntimeError as e:
        # RuntimeError from validate.py = FAILED data quality check.
        # Re-raise with context so the top-level handler can distinguish it.
        print(f"\n  ✗ {name} raised a validation failure:")
        print(f"    {e}")
        raise
    except Exception as e:
        print(f"\n  ✗ {name} failed with an unexpected error:")
        print(f"    {type(e).__name__}: {e}")
        raise


# Pipeline definition

# Ordered list of (label, callable) pairs.
# Each callable must be a zero-argument function (the module's run()).
STAGES = [
    ("1. Ingest",          ingest.run),
    ("2. Validate",        validate.run),
    ("3. Feature Engineering", risk_features.run),
    ("4. Cluster Model",   cluster_model.run),
    ("5. Risk Scorer",     risk_scorer.run),
]


# ---------------------------- Main entry point ----------------------------

def main() -> None:
    pipeline_start = log_pipeline_start()
    total = len(STAGES)

    for i, (name, fn) in enumerate(STAGES, start=1):
        try:
            run_stage(name, fn, i, total)

        except RuntimeError:
            # Validation FAILED - halt the pipeline cleanly.
            print("\n  Pipeline halted after validation failure.")
            print("  Check the audit_log table for details:")
            print("    SELECT * FROM audit_log ORDER BY run_id DESC LIMIT 20;")
            log_pipeline_end(time.time() - pipeline_start, success=False)
            sys.exit(1)

        except Exception:
            # Any other stage failure - halt and show the error.
            print(f"\n  Pipeline halted at stage: {name}")
            print("  Fix the error above and re-run.")
            log_pipeline_end(time.time() - pipeline_start, success=False)
            sys.exit(1)

    log_pipeline_end(time.time() - pipeline_start, success=True)

    print("  Output files ready for Power BI:")
    print(f"    data/exports/risk_scores.csv")
    print(f"    data/processed/risk_features_raw.csv")
    print(f"    data/processed/clustered_risk_features.csv")
    print(f"    db/blindspot.db  (domains + coverage_metrics + audit_log)\n")


if __name__ == "__main__":
    main()
