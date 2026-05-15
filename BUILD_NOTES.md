# Build Notes

A record of what was built and why, captured during the initial implementation session.

## Goal

A self-hosted dashboard giving a holistic view of Gemini and Vertex AI Model Garden usage in any GCP project the current user has access to. Runs locally or on a GCP VM, authenticates via `gcloud` (ADC), refreshes on demand.

## Required metrics

- % Queries by Model Name (doughnut)
- % Queries by Region (doughnut)
- # of Total Queries (stat card)
- Daily Average Queries (stat card)
- Peak Queries per Min (stat card)
- Average Queries per Min (stat card)
- % Queries by Response Code (doughnut)
- Per-model breakdown table: model name, peak/avg input tokens per min, peak/avg queries per min, total queries

Selectable timeframes: last hour, last 24 hours, last 7 days, last 30 days. Refresh on timeframe change.

## Architecture decisions

| Decision | Choice | Reason |
|---|---|---|
| Frontend stack | Vanilla HTML + Chart.js (CDN) | No build step, no node_modules, every line readable. Personal dashboard scope. |
| Backend stack | Python + FastAPI | Mature Google Cloud SDKs are Python-first. FastAPI gives async + auto query validation. |
| Auth | Application Default Credentials | Single source of truth for both local (`gcloud auth application-default login`) and VM (attached service account) — no app-side auth code needed. |
| Project list source | Cloud Resource Manager `search_projects` | Returns every project the caller has `resourcemanager.projects.get` on — matches "based on user's current GCP access". `list_projects` would require a parent org/folder. |
| Metric source | `aiplatform.googleapis.com/publisher/online_serving/{model_invocation_count, token_count}` | These are the publisher-model metrics emitted on every Gemini / Model Garden call. Carry `model_user_id`, `location`, `response_code` labels — covers every chart. |
| API shape | One `/api/metrics` endpoint, 4 parallel Monitoring queries server-side | Frontend does one fetch; backend uses `asyncio.gather` so total wall time ≈ slowest single call. |
| Storage | None — every refresh hits Cloud Monitoring fresh | Personal scale; reads are not separately billed. Caching can be added later if hammering refresh hits quotas. |

## Cloud Monitoring query design

All four queries use `ALIGN_DELTA` at a 60-second alignment period. Because `model_invocation_count` is a DELTA INT64 cumulative metric, `ALIGN_DELTA` returns "events in this 60-second bucket" — which is literally queries per minute, no extra math needed.

| # | Metric | Filter | Group by | Powers |
|---|---|---|---|---|
| A | `model_invocation_count` | (none) | `resource.label.model_user_id` | total queries, daily avg, peak QPM, avg QPM, % by model, table QPM cols |
| B | `model_invocation_count` | (none) | `resource.label.location` | % by region |
| C | `model_invocation_count` | (none) | `metric.label.response_code` | % by response code |
| D | `token_count` | `metric.label.type = "input"` | `resource.label.model_user_id` | table input-token cols |

Query window ends at `now() - 5 minutes` to skip Cloud Monitoring's ingestion lag (otherwise trailing buckets read as 0 and skew peak/avg).

## File layout

```
vertexai-dashboard/
├── README.md                  # setup, VM deployment, IAM perms
├── BUILD_NOTES.md             # this file
├── requirements.txt           # 5 pinned deps
├── run.sh                     # uvicorn launcher
├── backend/
│   ├── __init__.py
│   ├── auth.py                # google.auth.default() + friendly error
│   ├── projects.py            # Resource Manager search_projects
│   ├── metrics.py             # 4 parallel Monitoring queries
│   ├── aggregations.py        # pure math: total/peak/avg/percent/table
│   └── main.py                # FastAPI routes + static mount
└── frontend/
    ├── index.html             # markup + Chart.js CDN
    ├── app.js                 # fetch + render (vanilla, no framework)
    └── styles.css             # dark theme, CSS Grid, responsive
```

## End-to-end request flow

1. `./run.sh` starts uvicorn → loads `backend.main` → constructs FastAPI app + mounts `/static`.
2. Browser hits `/` → FastAPI returns `frontend/index.html`.
3. `index.html` loads Chart.js (CDN), `styles.css`, `app.js`.
4. `app.js` `init()` → `GET /api/projects`.
5. Route handler calls `auth.get_identity()` → `google.auth.default()` reads ADC. Then `projects.list_projects(creds)` → `ProjectsClient.search_projects()`. Returns `[{project_id, display_name}]`.
6. Dropdown populated, first project selected, `refresh()` fires.
7. `refresh()` → `GET /api/metrics?project_id=X&timeframe=24h`.
8. Route handler calls `metrics.fetch_dashboard_metrics()` → 4 `asyncio.to_thread` tasks, joined by `asyncio.gather`. Each task constructs a `MetricServiceClient`, calls `list_time_series`, normalizes the protobuf into `{labels, points}` dicts.
9. Results passed to `aggregations.py` helpers. Final dict shaped to match exactly what the frontend will render.
10. JSON returns. `app.js` updates 4 stat cards, 3 doughnuts (in-place via `chart.update()`), per-model table.
11. Change project or timeframe → step 7 fires again.

## Key implementation details worth remembering

- **`peak_per_minute` sums across series at the same timestamp first.** Otherwise a model serving 100 QPM in us-east1 and 50 QPM in us-central1 at the same minute would report a peak of 100, not 150.
- **`average_per_minute` skips zero-valued buckets.** A 7-day window with traffic only in one hour would otherwise dilute the average to ~0 and report nothing useful.
- **Chart.js instances are reused, not recreated.** `new Chart()` on the same canvas without `.destroy()` first leaks memory and causes flicker. The `state.charts` map tracks live instances and `renderDoughnut` mutates them in place.
- **Server binds to `127.0.0.1` only.** No app-side auth — exposing port 8000 to the network would let anyone on the network hit your GCP credentials. README documents the SSH tunnel pattern for VM deployment.
- **FastAPI query-param validation** (`Query(..., pattern="^(1h|24h|7d|30d)$")`) rejects bogus timeframes at the HTTP boundary before any code in `metrics.py` runs.

## IAM permissions required

On every project the user wants to view:

- `roles/monitoring.viewer` — to call `projects.timeSeries.list`
- `resourcemanager.projects.get` — granted by most basic roles, including `roles/viewer` and `roles/browser`

APIs that must be enabled on each viewed project:

- Cloud Resource Manager API (`cloudresourcemanager.googleapis.com`)
- Cloud Monitoring API (`monitoring.googleapis.com`)
- Vertex AI API (`aiplatform.googleapis.com`) — only needed for metrics to be *generated*; the dashboard itself never calls Vertex AI

## Known limitations

- **5-minute lag.** Queries you ran in the last ~5 min won't appear yet (Cloud Monitoring ingestion).
- **Custom-deployed models out of scope.** Only Model Garden / publisher models emit the `publisher/online_serving/*` metrics. Custom-trained endpoints use a different metric family (`prediction/online/prediction_count`).
- **No caching.** Hammering refresh on the largest timeframe (30 days, 60s buckets ≈ 43k points per series) may hit Monitoring API per-minute quotas. Easy to add a short TTL cache later if needed.
- **No app-side auth.** The dashboard inherits whatever GCP identity ADC resolves; access control is the host-binding (`127.0.0.1`) plus your machine's local users.

## Verification performed

- `pip install -r requirements.txt` succeeded.
- `from backend import main, auth, projects, metrics, aggregations` imports cleanly.
- `uvicorn backend.main:app` boots; `/`, `/static/app.js`, `/openapi.json` all return 200; `/api/metrics` rejects missing/bad query params with 422.
- `aggregations.py` math sanity-checked against synthetic data — totals, peaks (with same-timestamp summing), averages, percents, and the per-model table all produce the expected values.

Live test against a real GCP project requires `gcloud auth application-default login` and a project that has actually invoked a Gemini model in the chosen window.
