"""Container HTTP healthcheck (exec form, shell-free).

Uses the PORT environment variable set by docker-compose:
- api       -> GET /health
- streamlit -> GET /_stcore/health
"""

import os
import sys
import urllib.request

port = os.environ.get("PORT", "8000")
path = "/_stcore/health" if port == "8501" else "/health"
url = f"http://127.0.0.1:{port}{path}"

try:
    with urllib.request.urlopen(url, timeout=4) as response:
        sys.exit(0 if response.status == 200 else 1)
except OSError:
    sys.exit(1)
