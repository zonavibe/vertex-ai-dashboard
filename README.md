# Vertex AI Usage Dashboard

A small, self-hosted dashboard that gives you a holistic view of your Gemini
and Vertex AI Model Garden usage in any GCP project you have access to.

All data comes from **Cloud Monitoring** in real time. Nothing is stored
locally.

## What it shows

- Total queries, daily average, peak QPM, average QPM (headline cards)
- % queries by model, by region, by HTTP response code (doughnuts)
- Per-model breakdown table: peak/avg input tokens per minute, peak/avg
  queries per minute, total queries

Selectable timeframe: **last hour, last 24 hours, last 7 days, last 30 days**.
Picking a different project or timeframe re-fetches automatically.

## Architecture

```
Browser  ──HTTP──>  FastAPI (uvicorn)  ──ADC──>  Cloud Monitoring
                                              \──>  Resource Manager
```

- **Backend**: Python + FastAPI. One endpoint (`/api/metrics`) fans out 4
  parallel Cloud Monitoring queries and assembles the dashboard payload.
- **Frontend**: vanilla HTML/JS + Chart.js v4 from a CDN. No build step.
- **Auth**: Application Default Credentials. Locally that means
  `gcloud auth application-default login`. On a GCE VM, it's the attached
  service account.

## Prerequisites

- Python 3.10+ (3.12 recommended)
- `gcloud` CLI installed and on `$PATH`
- The following APIs enabled on every project you want to view:
  - Cloud Resource Manager API (`cloudresourcemanager.googleapis.com`)
  - Cloud Monitoring API (`monitoring.googleapis.com`)
  - Vertex AI API (`aiplatform.googleapis.com`) — only the projects you're
    *measuring* need this; the dashboard itself doesn't call Vertex AI.
- Your user (or service account) needs:
  - `roles/monitoring.viewer` on each project you want metrics from
  - `resourcemanager.projects.get` on each project (granted by most basic
    roles, including `roles/viewer`)

## Local setup

```bash
cd vertexai-dashboard

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

gcloud auth application-default login

./run.sh
```

Open <http://localhost:8000>.

## Running on a GCE VM

1. SSH into the VM.
2. Clone this repo and `pip install -r requirements.txt` as above.
3. Either:
   - Attach a service account to the VM that has `roles/monitoring.viewer`
     and `roles/browser` on the projects you want to view, **or**
   - Run `gcloud auth application-default login --no-launch-browser` and
     follow the URL prompt.
4. Edit `run.sh` to drop `--reload` and (if you want to access it from your
   laptop) bind to `0.0.0.0` instead of `127.0.0.1`. Be aware: the dashboard
   has no authentication of its own, so use a firewall rule, IAP, or an SSH
   tunnel rather than exposing port 8000 to the internet.
5. `./run.sh`.

The simplest secure access pattern from a laptop:

```bash
gcloud compute ssh YOUR_VM --zone=YOUR_ZONE -- -L 8000:localhost:8000
```

then visit <http://localhost:8000> on your laptop.

## File layout

```
vertexai-dashboard/
├── README.md
├── requirements.txt
├── run.sh
├── backend/
│   ├── __init__.py
│   ├── main.py          # FastAPI routes
│   ├── auth.py          # ADC resolution
│   ├── projects.py      # Resource Manager: list visible projects
│   ├── metrics.py       # Cloud Monitoring: fan out 4 queries
│   └── aggregations.py  # pure math: totals, peaks, percents
└── frontend/
    ├── index.html       # markup
    ├── app.js           # fetch + render
    └── styles.css       # dark theme
```

## How the metrics work

Vertex AI emits these Cloud Monitoring metrics on every publisher-model
invocation (Gemini, Anthropic, etc.):

- `aiplatform.googleapis.com/publisher/online_serving/model_invocation_count`
- `aiplatform.googleapis.com/publisher/online_serving/token_count`

Both are DELTA INT64 metrics on the `aiplatform.googleapis.com/PublisherModel`
resource type, and carry labels for `model_user_id` (e.g. `gemini-1.5-pro`),
`location` (region), `response_code`, and (for tokens) `type` (input/output).

The dashboard runs four queries with `ALIGN_DELTA` at a 60s alignment period:

1. invocations grouped by `model_user_id` → headline cards, % by model, table
2. invocations grouped by `location` → % by region
3. invocations grouped by `response_code` → % by response code
4. input tokens grouped by `model_user_id` → table token columns

All four run in parallel via `asyncio.gather`. Total wall time ≈ slowest
single call.

## Notes and gotchas

- **Ingestion lag**: Cloud Monitoring data arrives ~3–4 minutes late, so the
  query window ends at `now − 5 min`. You won't see queries you ran 30
  seconds ago.
- **No local cache**: every refresh hits Cloud Monitoring. Reads on
  `projects.timeSeries.list` are not separately billed, but if you hammer
  refresh you may hit per-minute API quotas. There's plenty of room for a
  short TTL cache later if you want one.
- **Empty doughnuts mean no data**: if a project genuinely had zero Gemini
  calls in the chosen window, the doughnuts will be empty. Try widening the
  timeframe.
- **Custom-deployed models** (not from Model Garden) don't emit publisher
  metrics — they use a different metric family. They're out of scope here.
