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
    totals_by_group: dict[str, float] = {}
    for s in series_list:
        group = s["labels"].get(label_key, "(unknown)")
        totals_by_group[group] = totals_by_group.get(group, 0) + sum(
            v for (_, v) in s["points"]
        )

    grand_total = sum(totals_by_group.values())
    if grand_total == 0:
        return {}

    return {
        group: round(value * 100 / grand_total, 2)
        for group, value in totals_by_group.items()
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
