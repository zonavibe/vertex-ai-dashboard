#!/usr/bin/env bash
# Local launcher. Listens only on 127.0.0.1 so the dashboard isn't exposed
# to your network. Drop --reload when running on a VM in production.
set -euo pipefail
cd "$(dirname "$0")"
exec uvicorn backend.main:app --host 127.0.0.1 --port 8000 --reload
