"""Live end-to-end test against the full docker-compose stack.

Requires the stack to be running (api + worker + postgres + rabbitmq):

    docker compose up -d --build
    RUN_LIVE_E2E=1 API_BASE_URL=http://localhost:8000 pytest tests/e2e/test_live_stack.py

Skipped automatically unless RUN_LIVE_E2E=1.
"""

import os
from datetime import date

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LIVE_E2E") != "1",
    reason="RUN_LIVE_E2E=1 and a running docker-compose stack are required",
)

BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:8000")
TAX_ID = f"LIVE{date.today().strftime('%y%m%d%H%M%S')}"[:12]


def build_real_pdf() -> bytes:
    """A real single-page PDF whose embedded text the deterministic LLM parses."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), (
        "INVOICE\n"
        "Number: INV-LIVE-001\n"
        "Date: 2026-01-15   Due: 2026-02-14\n"
        "Supplier: LiveTest Supplier S.L.  Tax ID: B12345678\n"
        "Item  Consulting  4 x 250.00\n"
        "Subtotal: 1000.00   Tax: 210.00   Total: 1210.00 EUR\n"
    ), fontsize=11)
    data = doc.tobytes()
    doc.close()
    return data


def test_full_live_flow():
    import httpx

    with httpx.Client(base_url=BASE_URL, timeout=30) as client:
        # 1) Health check proves DB connectivity of the live API.
        health = client.get("/health")
        assert health.status_code == 200
        assert health.json()["components"]["database"] == "up"

        # 2) Upload a real PDF -> 202 + poll URL.
        upload = client.post(
            "/api/v1/invoices/upload",
            files={"file": ("live-invoice.pdf", build_real_pdf(), "application/pdf")},
        )
        assert upload.status_code == 202, upload.text
        poll_url = upload.json()["poll_url"]

        # 3) Poll until the Celery worker finishes (bounded wait).
        job = None
        for _ in range(30):
            poll = client.get(poll_url)
            assert poll.status_code == 200
            job = poll.json()
            if job["status"] in {"COMPLETED", "FAILED"}:
                break
            import time

            time.sleep(1)
        assert job is not None and job["status"] == "COMPLETED", job
        invoice_id = job["invoice_id"]
        assert invoice_id is not None

        # 4) Invoice detail is queryable with supplier + items.
        detail = client.get(f"/api/v1/invoices/{invoice_id}")
        assert detail.status_code == 200
        invoice = detail.json()
        assert invoice["number"] == "INV-2026-001"  # deterministic mock extractor
        assert len(invoice["items"]) >= 1

        # 5) Search & dashboard reflect the new invoice.
        search = client.get("/api/v1/invoices", params={"search": "B12345678"})
        assert search.json()["total"] >= 1

        stats = client.get("/api/v1/dashboard/stats").json()
        assert stats["invoices"]["total"] >= 1
