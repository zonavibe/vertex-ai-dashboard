"""
FastAPI app — wires routes to the auth, projects, and metrics modules and
serves the static frontend.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.api_core.exceptions import GoogleAPICallError, PermissionDenied

from . import auth, metrics, projects


app = FastAPI(title="Vertex AI Usage Dashboard")

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"


# ---------- Frontend ----------

# Static assets (app.js, styles.css). The HTML itself is served by the root
# route below so we can return a clean URL ("/") instead of "/index.html".
app.mount(
    "/static",
    StaticFiles(directory=FRONTEND_DIR),
    name="static",
)


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


# ---------- API ----------


@app.get("/api/projects")
def api_projects() -> list[dict]:
    """List the GCP projects the current ADC identity can see."""
    try:
        identity = auth.get_identity()
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    try:
        return projects.list_projects(identity.credentials)
    except PermissionDenied as e:
        raise HTTPException(
            status_code=403,
            detail=f"Resource Manager denied access: {e.message}. "
            "Make sure the Cloud Resource Manager API is enabled and your "
            "user has resourcemanager.projects.get on at least one project.",
        )


@app.get("/api/metrics")
async def api_metrics(
    project_id: str = Query(..., min_length=1),
    timeframe: str = Query("24h", pattern="^(1h|24h|7d|30d)$"),
) -> dict:
    """Fan out the 4 Monitoring queries and return the dashboard payload."""
    try:
        identity = auth.get_identity()
    except auth.AuthError as e:
        raise HTTPException(status_code=401, detail=str(e))

    try:
        return await metrics.fetch_dashboard_metrics(
            credentials=identity.credentials,
            project_id=project_id,
            timeframe=timeframe,
        )
    except PermissionDenied as e:
        raise HTTPException(
            status_code=403,
            detail=f"Cloud Monitoring denied access on {project_id}: {e.message}. "
            "Make sure the Monitoring API is enabled on the project and your "
            "user has roles/monitoring.viewer.",
        )
    except GoogleAPICallError as e:
        # Catch-all for other API errors (5xx, quota, etc).
        raise HTTPException(status_code=502, detail=f"GCP API error: {e.message}")


# ---------- Error formatting ----------


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException):
    """Ensure every error comes back as {"error": "..."} JSON so the
    frontend can render it uniformly."""
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})
