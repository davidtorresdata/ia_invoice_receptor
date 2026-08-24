"""Dashboard page: processing statistics from the API."""

import api_client
import streamlit as st

client = api_client.get_api_client()

st.title("📊 Dashboard")

if st.button("Refresh", type="primary"):
    st.rerun()

try:
    stats = client.stats()
except api_client.ApiError as exc:
    st.error(str(exc))
    st.stop()

jobs = stats["jobs"]
invoices = stats["invoices"]
total_invoiced = stats["total_invoiced"]

col_a, col_b = st.columns(2)
col_a.metric("Total invoices", invoices["total"])
col_b.metric("Total invoiced", f"{total_invoiced:,.2f}")

st.divider()
left, right = st.columns(2)

with left:
    st.subheader("Jobs")
    j1, j2 = st.columns(2)
    j1.metric("Pending", jobs["pending"])
    j2.metric("Processing", jobs["processing"])
    j3, j4 = st.columns(2)
    j3.metric("Completed", jobs["completed"])
    j4.metric("Failed", jobs["failed"])

with right:
    st.subheader("Invoices")
    if invoices["total"]:
        st.info(f"{invoices['total']} invoice(s) processed.")
    else:
        st.info("No invoices processed yet. Upload one in **Upload Invoice**.")

st.caption(
    "Data served by FastAPI (`GET /api/v1/dashboard/stats`). "
    "The UI never accesses the database directly."
)
