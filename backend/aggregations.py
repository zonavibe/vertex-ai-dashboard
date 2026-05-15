"""
Pure aggregation helpers.

The metrics module returns time series in a normalized shape:
    Series = {
        "labels": {label_name: label_value, ...},   # e.g. {"model_user_id": "gemini-1.5-pro"}
        "points": [(timestamp, value), ...],         # one point per 60s bucket
    }

Functions here turn a list[Series] into the numbers and shapes the frontend
needs. Keeping these as pure functions (no I/O, no SDK objects) makes them
trivial to test and reason about.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable


# Approximate days for each timeframe — used for "Daily Average".
TIMEFRAME_DAYS = {
    "1h": 1 / 24,
    "24h": 1.0,
    "7d": 7.0,
    "30d": 30.0,
}


def _all_values(series_list: Iterable[dict]) -> list[float]:
    """Flatten all (timestamp, value) points across all series into one list of values."""
    return [v for s in series_list for (_, v) in s["points"]]


def _totals_by_group(series_list: Iterable[dict], label_key: str) -> dict[str, float]:
    """Sum every point in every series, bucketed by the given label."""
    out: dict[str, float] = {}
    for s in series_list:
        group = s["labels"].get(label_key, "(unknown)")
        out[group] = out.get(group, 0) + sum(v for (_, v) in s["points"])
    return out


def total(series_list: list[dict]) -> int:
    """Sum every point across every series. With ALIGN_DELTA at 60s, each
    point is "events in that minute", so summing gives the total over the
    whole window."""
    return int(sum(_all_values(series_list)))


def peak_per_minute(series_list: list[dict]) -> int:
    """The single largest 60-second bucket across the whole window.

    We sum across series at the same timestamp first — otherwise a model
    that handled 100 QPM in us-east1 and 50 QPM in us-central1 at the same
    minute would report a peak of 100 instead of 150."""
    by_timestamp: dict = {}
    for s in series_list:
        for ts, v in s["points"]:
            by_timestamp[ts] = by_timestamp.get(ts, 0) + v
    return int(max(by_timestamp.values(), default=0))


def average_per_minute(series_list: list[dict]) -> float:
    """Average over minutes that actually had traffic.

    We deliberately skip zero-valued buckets. If you queried a 7-day window
    and traffic only happened in one hour, including the other ~10,000 zero
    buckets would dilute the average to ~0 and tell you nothing useful."""
    by_timestamp: dict = {}
    for s in series_list:
        for ts, v in s["points"]:
            by_timestamp[ts] = by_timestamp.get(ts, 0) + v

    nonzero = [v for v in by_timestamp.values() if v > 0]
    if not nonzero:
        return 0.0
    return round(sum(nonzero) / len(nonzero), 2)


def daily_average(series_list: list[dict], timeframe: str) -> float:
    """Total queries divided by number of days in the chosen window."""
    days = TIMEFRAME_DAYS.get(timeframe, 1.0)
    return round(total(series_list) / days, 2)


def percent_by_group(series_list: list[dict], label_key: str) -> dict[str, float]:
    """Group series by the named label and return {group: percent_of_total}.

    Used for the three doughnut charts. The frontend renders each entry as
    one slice; the legend shows the group name and percent."""
    totals = _totals_by_group(series_list, label_key)
    grand_total = sum(totals.values())
    if grand_total == 0:
        return {}

    return {
        group: round(value * 100 / grand_total, 2)
        for group, value in totals.items()
    }


def per_model_table(
    invocation_series: list[dict],
    token_series: list[dict],
) -> list[dict]:
    """Build the per-model table.

    We have two parallel sets of series — invocations and input tokens — both
    grouped by `model_user_id`. We bucket each by model name, then compute
    peak/avg/total per model. Models that show up in only one of the two
    datasets still get a row (the missing column reads as 0)."""

    def bucket(series_list: list[dict]) -> dict[str, list[dict]]:
        out: dict[str, list[dict]] = {}
        for s in series_list:
            name = s["labels"].get("model_user_id", "(unknown)")
            out.setdefault(name, []).append(s)
        return out

    inv_by_model = bucket(invocation_series)
    tok_by_model = bucket(token_series)

    rows = []
    for model_name in sorted(set(inv_by_model) | set(tok_by_model)):
        inv = inv_by_model.get(model_name, [])
        tok = tok_by_model.get(model_name, [])
        rows.append(
            {
                "model_name": model_name,
                "peak_input_tokens_per_min": peak_per_minute(tok),
                "avg_input_tokens_per_min": average_per_minute(tok),
                "peak_queries_per_min": peak_per_minute(inv),
                "avg_queries_per_min": average_per_minute(inv),
                "total_queries": total(inv),
            }
        )

    # Most-used models on top.
    rows.sort(key=lambda r: r["total_queries"], reverse=True)
    return rows


def tokens_per_day_by_model(token_series_list: list[dict]) -> dict:
    """Bucket per-minute token points into per-day-per-model totals.

    Returns a structure shaped for the grouped bar chart:
        {
            "days":   ["2026-05-08", "2026-05-09", ...],
            "models": ["gemini-2.5-flash", "gemini-2.5-pro", ...],
            "matrix": {model_name: [tokens_on_day_0, tokens_on_day_1, ...]},
        }

    The day index in `matrix[model]` lines up with the same index in `days`,
    so the frontend can render one Chart.js dataset per model with values
    pulled directly off `matrix[model]` and the x-axis from `days`."""
    by_day_model: dict[str, dict[str, float]] = {}

    for s in token_series_list:
        model = s["labels"].get("model_user_id", "(unknown)")
        for ts, v in s["points"]:
            # Bucket by UTC date — Cloud Monitoring timestamps are UTC,
            # and rendering UTC dates avoids any timezone surprises when
            # the user is on cloudshell vs. their laptop.
            day = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")
            day_bucket = by_day_model.setdefault(day, {})
            day_bucket[model] = day_bucket.get(model, 0) + v

    days = sorted(by_day_model.keys())
    models = sorted({m for d in by_day_model.values() for m in d})
    matrix = {
        m: [int(round(by_day_model[d].get(m, 0))) for d in days]
        for m in models
    }
    return {"days": days, "models": models, "matrix": matrix}


def avg_tokens_per_query_by_model(
    token_series_list: list[dict],
    invocation_series_list: list[dict],
) -> dict[str, float]:
    """For each model, total_tokens / total_queries.

    Models with zero queries are skipped (avoids divide-by-zero and is the
    correct semantic — "average tokens per query" is undefined when there
    are no queries)."""
    tokens = _totals_by_group(token_series_list, "model_user_id")
    queries = _totals_by_group(invocation_series_list, "model_user_id")

    return {
        model: round(tokens.get(model, 0) / q, 2)
        for model, q in queries.items()
        if q > 0
    }
