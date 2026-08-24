"""Upload page: file upload + async job polling (all through the API)."""

import time

import api_client
import streamlit as st

client = api_client.get_api_client()

st.title("📤 Upload Invoice")
st.caption("Accepted formats: PDF, PNG, JPG, JPEG — processing runs asynchronously.")


def poll_job(job_id: str) -> None:
    """Poll the jobs endpoint until a terminal state (UI concern only)."""
    placeholder = st.empty()
    progress = st.progress(0.0, text="Waiting for worker…")
    started = time.monotonic()
    deadline = started + 300  # hard cap: 5 minutes

    while time.monotonic() < deadline:
        try:
            job = client.get_job(job_id)
        except api_client.ApiError as exc:
            placeholder.error(f"Status check failed: {exc}")
            return

        status = job["status"]
        if status == "COMPLETED":
            progress.progress(1.0, text="Done")
            placeholder.success("Invoice processed successfully.")
            if job.get("invoice_id"):
                st.session_state["selected_invoice_id"] = job["invoice_id"]
                if st.button("Open invoice detail"):
                    st.switch_page("pages/invoice_detail.py")
            return
        if status == "FAILED":
            progress.empty()
            message = job.get("error_message") or "unknown error"
            details = [part.strip(" .") for part in message.split("; ") if part.strip()]
            if len(details) > 1:
                placeholder.error("**Processing failed:**")
                for part in details:
                    st.markdown(f"- {part}")
            else:
                placeholder.error(f"Processing failed: {message}")
            return

        elapsed_ratio = min((time.monotonic() - started) / 300, 0.95)
        progress.progress(elapsed_ratio,
                          text=f"Status: {status} (attempt {job['attempts']})")
        time.sleep(2)

    placeholder.warning("Still processing after 5 minutes — retry later from **Invoices**.")


uploaded = st.file_uploader("Invoice document", type=["pdf", "png", "jpg", "jpeg"])

if uploaded is not None and uploaded.size > 0:
    size_mb = uploaded.size / (1024 * 1024)
    st.info(f"`{uploaded.name}` — {size_mb:.2f} MB — `{uploaded.type or 'unknown'}`")

    if st.button("Process invoice", type="primary"):
        try:
            with st.spinner("Uploading…"):
                result = client.upload(
                    filename=uploaded.name,
                    content=uploaded.getvalue(),
                    content_type=uploaded.type or "application/octet-stream",
                )
        except api_client.ApiError as exc:
            st.error(f"Upload rejected: {exc}")
            st.stop()

        st.session_state["last_job_id"] = result["job_id"]
        st.success(f"Queued! **job_id:** `{result['job_id']}`")
        poll_job(result["job_id"])
