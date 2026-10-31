"""CloudWatch metrics as log lines (embedded metric format, DV9)."""

import json
import time


def emf_line(
    namespace: str,
    dimensions: dict[str, str],
    metrics: dict[str, tuple[float, str]],
    timestamp_ms: int,
) -> str:
    # CloudWatch reads this line from the container log and makes the metrics itself,
    # so no PutMetricData permission or extra API call is needed.
    directive = {
        "Namespace": namespace,
        "Dimensions": [list(dimensions)],
        "Metrics": [{"Name": name, "Unit": unit} for name, (_, unit) in metrics.items()],
    }
    document = {
        "_aws": {"Timestamp": timestamp_ms, "CloudWatchMetrics": [directive]},
        **dimensions,
        **{name: value for name, (value, _) in metrics.items()},
    }
    return json.dumps(document, separators=(",", ":"))  # one line, no newlines


def emit(dimensions: dict[str, str], metrics: dict[str, tuple[float, str]]) -> None:
    # flush so the line reaches the container log now and not when a buffer fills.
    print(emf_line("PromptGate", dimensions, metrics, int(time.time() * 1000)), flush=True)
