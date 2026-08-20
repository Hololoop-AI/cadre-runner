"""Signal logging: MLflow when available (the decided spine — see
cadre-context decisions/2026-08-19-eval-stack-mlflow.md), JSONL always.
The JSONL is the durable local record; MLflow is the queryable/optimizable
view. Core stays importable without mlflow installed."""

import json
import os
import time
from pathlib import Path

MLFLOW_URI = os.environ.get(
    "CADRE_MLFLOW_URI",
    f"sqlite:///{Path.home()}/.local/state/pipeline-runner/mlflow.db")  # file backend is in maintenance mode
JSONL = Path(os.environ.get("CADRE_SIGNALS_JSONL",
                            str(Path.home() / ".local/state/pipeline-runner/signals.jsonl")))


def log_trial(kind: str, name: str, model: str, checks, metrics: dict, meta: dict | None = None):
    """One eval trial: pass/fail claims + graded metrics. Returns the record."""
    rec = {
        "ts": time.time(), "kind": kind, "name": name, "model": model,
        "passed": all(ok for _, ok, _ in checks),
        "claims": {n: ok for n, ok, _ in checks},
        "failures": {n: d for n, ok, d in checks if not ok},
        "metrics": metrics, **(meta or {}),
    }
    JSONL.parent.mkdir(parents=True, exist_ok=True)
    with open(JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    try:
        import mlflow
        mlflow.set_tracking_uri(MLFLOW_URI)
        mlflow.set_experiment(f"cadre-{kind}")
        with mlflow.start_run(run_name=f"{name}-{time.strftime('%m%d-%H%M%S')}"):
            mlflow.log_params({"name": name, "model": model, **{k: str(v) for k, v in (meta or {}).items()}})
            mlflow.log_metrics({"passed": 1.0 if rec["passed"] else 0.0,
                                **{k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}})
            for n, ok, _ in checks:
                mlflow.log_metric(f"claim.{n.replace('<','_lt_').replace('=','_eq_').replace(':','.')}",
                                  1.0 if ok else 0.0)
    except ImportError:
        rec["mlflow"] = "not installed — jsonl only"
    except Exception as e:  # tracking must never fail a trial
        rec["mlflow_error"] = str(e)[:200]
    return rec
