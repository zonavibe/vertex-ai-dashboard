"""
List the GCP projects the current user has access to.

We use Resource Manager's `search_projects` (not `list_projects`). The
difference matters: `list_projects` requires a parent (org or folder) and the
`resourcemanager.projects.list` permission on it. `search_projects` returns
every project where the caller has `resourcemanager.projects.get`, which is
what "what can this user actually see?" should mean.
"""

from __future__ import annotations

from google.cloud import resourcemanager_v3


def list_projects(credentials) -> list[dict]:
    client = resourcemanager_v3.ProjectsClient(credentials=credentials)

    # search_projects() returns a paginated iterator. Iterating it transparently
    # makes additional API calls under the hood. For typical users (< few hundred
    # projects) this completes in one or two round trips.
    projects = []
    for project in client.search_projects():
        # Filter to ACTIVE — DELETE_REQUESTED projects are still returned
        # for ~30 days and would clutter the dropdown.
        if project.state != resourcemanager_v3.Project.State.ACTIVE:
            continue

        projects.append(
            {
                "project_id": project.project_id,
                "display_name": project.display_name or project.project_id,
            }
        )

    # Sort alphabetically by display name so the dropdown is predictable.
    projects.sort(key=lambda p: p["display_name"].lower())
    return projects
