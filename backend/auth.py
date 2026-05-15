"""
Authentication helper.

We use Application Default Credentials (ADC). That means: whatever identity
you set up with `gcloud auth application-default login` (when running locally)
or the attached service account (when running on a GCE VM) is automatically
discovered by google.auth.default().
"""

from __future__ import annotations

from dataclasses import dataclass

import google.auth
from google.auth.exceptions import DefaultCredentialsError


class AuthError(Exception):
    """Raised when ADC cannot be resolved. The route layer turns this into
    an HTTP 401 with a friendly message."""


@dataclass
class Identity:
    credentials: object  # google.auth.credentials.Credentials
    quota_project_id: str | None  # the project ADC will bill API calls to


def get_identity() -> Identity:
    """Resolve ADC and return the credentials plus the quota project.

    google.auth.default() searches in this order:
      1. GOOGLE_APPLICATION_CREDENTIALS env var (service-account key file)
      2. ~/.config/gcloud/application_default_credentials.json (gcloud login)
      3. GCE/GKE/Cloud Run metadata server (when running on GCP)

    The second one is what `gcloud auth application-default login` writes,
    which is the path you'll hit when running this dashboard locally.
    """
    try:
        credentials, quota_project_id = google.auth.default()
    except DefaultCredentialsError as e:
        raise AuthError(
            "No Google Cloud credentials found. Run:\n"
            "    gcloud auth application-default login\n"
            "and then reload the page."
        ) from e

    return Identity(credentials=credentials, quota_project_id=quota_project_id)
