"""Streamlit entrypoint — multi-page shell.

The UI contains ZERO business logic: every screen renders data fetched from
the FastAPI service through `api_client`.
"""

import os
import sys
from pathlib import Path

import streamlit as st

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.config.settings import get_settings  # noqa: E402
from app.infrastructure.logging_setup import configure_logging  # noqa: E402

# Same traceable formatter as api/worker; excepthooks capture any unhandled
# page error into the container logs with module/file/function/line.
_settings = get_settings()
configure_logging(level=_settings.log_level, log_format=_settings.log_format)

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")

st.set_page_config(
    page_title="Invoice Processing",
    page_icon="🧾",
    layout="wide",
    initial_sidebar_state="expanded",
)

dashboard = st.Page("pages/dashboard.py", title="Dashboard", icon="📊", default=True)
upload = st.Page("pages/upload_invoice.py", title="Upload Invoice", icon="📤")
invoices = st.Page("pages/invoices.py", title="Invoices", icon="🧾")
detail = st.Page("pages/invoice_detail.py", title="Invoice Detail", icon="🔎")

navigation = st.navigation([dashboard, upload, invoices, detail])

with st.sidebar:
    st.title("🧾 Invoice Processor")
    st.caption(f"API: `{API_BASE_URL}`")
    st.divider()
    st.caption(
        "Upload → OCR → LLM → validation → PostgreSQL. "
        "Heavy work runs in Celery workers."
    )

navigation.run()
