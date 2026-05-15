"""
Cloud Monitoring queries.

We pull every datapoint from the publisher metric family that Vertex AI emits
when a foundation model is invoked. Five queries cover everything the
dashboard needs:

    A. model_invocation_count grouped by model_user_id    (per-model)
    B. model_invocation_count grouped by location         (per-region)
    C. model_invocation_count grouped by response_code    (per-status)
    D. token_count, type=input, grouped by model_user_id  (per-model input tokens)
    E. token_count (no type filter) grouped by model_user_id
       (per-model total tokens — input + output, summed by REDUCE_SUM —
        powers the two bottom bar charts)

All five run in parallel via asyncio.gather, since each is an independent
network call.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from google.cloud import monitoring_v3
from google.protobuf import duration_pb2, timestamp_pb2

from . import aggregations


METRIC_INVOCATION = "aiplatform.googleapis.com/publisher/online_serving/model_invocation_count"
METRIC_TOKEN = "aiplatform.googleapis.com/publisher/online_serving/token_count"

# How far back the window starts, by timeframe key.
TIMEFRAME_DELTAS = {
    "1h": timedelta(hours=1),
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}

# Cloud Monitoring has ~3-4 minutes of ingestion lag for these metrics.
# Ending the query at "now - 5 min" avoids the trailing buckets reading as
# zero (which would skew "peak" downward and dilute averages).
INGESTION_LAG = timedelta(minutes=5)


def _interval(timeframe: str) -> monitoring_v3.TimeInterval:
    end = datetime.now(timezone.utc) - INGESTION_LAG
    start = end - TIMEFRAME_DELTAS[timeframe]

    end_pb = timestamp_pb2.Timestamp()
    end_pb.FromDatetime(end)
    start_pb = timestamp_pb2.Timestamp()
    start_pb.FromDatetime(start)
    return monitoring_v3.TimeInterval(start_time=start_pb, end_time=end_pb)


def _aggregation(group_by: list[str]) -> monitoring_v3.Aggregation:
    """Bucket points into 60-second windows, sum across same-bucket series
    that share the group-by labels.

    ALIGN_DELTA on a DELTA-kind metric returns the count that occurred during
    each 60-second bucket. That is literally "queries per minute", which is
    exactly the unit we want to render."""
    period = duration_pb2.Duration(seconds=60)
    return monitoring_v3.Aggregation(
        alignment_period=period,
        per_series_aligner=monitoring_v3.Aggregation.Aligner.ALIGN_DELTA,
        cross_series_reducer=monitoring_v3.Aggregation.Reducer.REDUCE_SUM,
        group_by_fields=group_by,
    )


def _normalize_series(time_series_iter, label_keys: list[str]) -> list[dict]:
    """Convert the SDK's TimeSeries protobuf objects into plain dicts.

    Aggregation strips out everything except the group_by_fields, so we only
    need to read the labels we asked to be grouped by. Resource labels and
    metric labels live in different sub-objects, hence the two lookups."""
    out = []
    for ts in time_series_iter:
        labels = {}
        for key in label_keys:
            # Strip the "resource.label." / "metric.label." prefix the API
            # uses in group_by_fields — we want the bare key in the output.
            short = key.split(".")[-1]
            labels[short] = (
                ts.resource.labels.get(short)
                or ts.metric.labels.get(short)
                or "(unknown)"
            )

        points = []
        for p in ts.points:
            # proto-plus surfaces Timestamps as DatetimeWithNanoseconds
            # (a datetime subclass), not raw protobuf Timestamp — so we
            # use .timestamp() to get unix seconds, not .seconds.
            ts_seconds = int(p.interval.end_time.timestamp())
            # model_invocation_count is INT64 and token_count is INT64,
            # but be defensive in case the API ever surfaces a DOUBLE
            # (e.g. a future ALIGN_RATE on a different metric). Both
            # unset proto fields default to 0, so the `or` chain
            # picks whichever one was actually populated.
            value = p.value.int64_value or p.value.double_value or 0
            points.append((ts_seconds, value))

        out.append({"labels": labels, "points": points})
    return out


def _query_sync(
    credentials,
    project_id: str,
    metric_filter: str,
    interval: monitoring_v3.TimeInterval,
    group_by: list[str],
) -> list[dict]:
    """Synchronous wrapper around list_time_series. Run via asyncio.to_thread."""
    client = monitoring_v3.MetricServiceClient(credentials=credentials)
    pager = client.list_time_series(
        request={
            "name": f"projects/{project_id}",
            "filter": metric_filter,
            "interval": interval,
            "aggregation": _aggregation(group_by),
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL,
        }
    )
    return _normalize_series(pager, group_by)


async def fetch_dashboard_metrics(
    credentials,
    project_id: str,
    timeframe: str,
) -> dict:
    """Run all 5 Monitoring queries in parallel and assemble the dashboard payload."""
    if timeframe not in TIMEFRAME_DELTAS:
        raise ValueError(f"Unknown timeframe: {timeframe!r}")

    interval = _interval(timeframe)
    started = time.monotonic()

    # Each task is a separate Cloud Monitoring API call. Running them
    # concurrently keeps the worst-case latency near the slowest single call
    # rather than the sum.
    by_model_task = asyncio.to_thread(
        _query_sync,
        credentials,
        project_id,
        f'metric.type = "{METRIC_INVOCATION}"',
        interval,
        ["resource.label.model_user_id"],
    )
    by_region_task = asyncio.to_thread(
        _query_sync,
        credentials,
        project_id,
        f'metric.type = "{METRIC_INVOCATION}"',
        interval,
        ["resource.label.location"],
    )
    by_response_task = asyncio.to_thread(
        _query_sync,
        credentials,
        project_id,
        f'metric.type = "{METRIC_INVOCATION}"',
        interval,
        ["metric.label.response_code"],
    )
    by_model_tokens_task = asyncio.to_thread(
        _query_sync,
        credentials,
        project_id,
        f'metric.type = "{METRIC_TOKEN}" AND metric.label.type = "input"',
        interval,
        ["resource.label.model_user_id"],
    )
    # Total tokens (input + output) per model. Powers the two new bar
    # charts. Same metric as above but with no `type` filter, so the
    # cross_series_reducer SUMs both input and output rows together.
    by_model_total_tokens_task = asyncio.to_thread(
        _query_sync,
        credentials,
        project_id,
        f'metric.type = "{METRIC_TOKEN}"',
        interval,
        ["resource.label.model_user_id"],
    )

    (
        by_model,
        by_region,
        by_response,
        by_model_tokens,
        by_model_total_tokens,
    ) = await asyncio.gather(
        by_model_task,
        by_region_task,
        by_response_task,
        by_model_tokens_task,
        by_model_total_tokens_task,
    )

    elapsed_ms = int((time.monotonic() - started) * 1000)

    return {
        "timeframe": timeframe,
        "project_id": project_id,
        "elapsed_ms": elapsed_ms,
        # Headline cards (computed from the per-model invocation series, which
        # covers all traffic regardless of region or status).
        "total_queries": aggregations.total(by_model),
        "daily_average": aggregations.daily_average(by_model, timeframe),
        "peak_queries_per_minute": aggregations.peak_per_minute(by_model),
        "avg_queries_per_minute": aggregations.average_per_minute(by_model),
        # Doughnuts (each is {label: percent}).
        "by_model": aggregations.percent_by_group(by_model, "model_user_id"),
        "by_region": aggregations.percent_by_group(by_region, "location"),
        "by_response_code": aggregations.percent_by_group(by_response, "response_code"),
        # Detail table.
        "per_model_table": aggregations.per_model_table(by_model, by_model_tokens),
        # Bottom bar charts (total tokens, input + output).
        "tokens_per_day_by_model": aggregations.tokens_per_day_by_model(
            by_model_total_tokens
        ),
        "avg_tokens_per_query_by_model": aggregations.avg_tokens_per_query_by_model(
            by_model_total_tokens, by_model
        ),
    }
