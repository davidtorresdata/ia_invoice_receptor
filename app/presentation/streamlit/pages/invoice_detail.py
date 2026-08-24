"""Invoice detail page: extracted data, supplier, items, validation report."""

import json

import api_client
import streamlit as st

client = api_client.get_api_client()

st.title("📄 Invoice Detail")

invoice_id = st.session_state.get("selected_invoice_id")
if not invoice_id:
    invoice_id = st.query_params.get("invoice")
if not invoice_id:
    st.info("Select an invoice from the **Invoices** page first.")
    st.stop()

try:
    invoice = client.get_invoice(invoice_id)
except api_client.ApiError as exc:
    if exc.status_code == 404:
        st.warning("Invoice not found (it may have been removed).")
    else:
        st.error(str(exc))
    st.stop()

# --------------------------------------------------------------------- header
st.subheader(f"{invoice['number']} — {invoice['supplier']['name']}")
st.caption(
    f"Issued {invoice['issue_date']}"
    + (f" · due {invoice['due_date']}" if invoice.get("due_date") else "")
)

m1, m2, m3, m4 = st.columns(4)
m1.metric("Total", f"{invoice['total']:,.2f} {invoice['currency']}")
m2.metric("Subtotal", f"{invoice['subtotal']:,.2f}")
m3.metric("Tax", f"{invoice['tax']:,.2f}")
m4.metric("Items", len(invoice["items"]))

st.divider()

# ------------------------------------------------------------- supplier block
left, right = st.columns([1, 1])
with left:
    st.markdown("#### Supplier")
    supplier = invoice["supplier"]
    st.markdown(
        f"""
| | |
|---|---|
| **Name** | {supplier['name']} |
| **Tax ID** | `{supplier['tax_id'] or '—'}` |
| **Address** | {supplier.get('address') or '—'} |
| **Phone** | {supplier.get('phone') or '—'} |
| **Email** | {supplier.get('email') or '—'} |
""",
        unsafe_allow_html=False,
    )
with right:
    st.markdown("#### Document")
    st.json(
        {
            "invoice_id": str(invoice["id"]),
            "document_id": str(invoice["document_id"]),
            "currency": invoice["currency"],
            "created_at": invoice["created_at"],
        }
    )

# ------------------------------------------------------------------ items
st.markdown("#### Line items")

import pandas as pd  # noqa: E402

items_frame = pd.DataFrame(
    [
        {
            "#": i + 1,
            "Description": item["description"],
            "Qty": float(item["quantity"]),
            "Unit price": item["unit_price"],
            "Tax": item["tax"],
            "Total": item["total"],
        }
        for i, item in enumerate(invoice["items"])
    ]
)
st.dataframe(
    items_frame,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Unit price": st.column_config.NumberColumn(format="%.2f"),
        "Tax": st.column_config.NumberColumn(format="%.2f"),
        "Total": st.column_config.NumberColumn(format="%.2f"),
    },
)
st.caption(
    f"Σ items: **{sum(i['total'] for i in invoice['items']):,.2f}** "
    f"(declared subtotal: {invoice['subtotal']:,.2f})"
)

# ------------------------------------------------------- validation & raw data
with st.expander("🔍 Validation report", expanded=False):
    report = invoice.get("validation_report") or {"is_valid": None, "issues": []}
    issues = report.get("issues", [])
    if not issues:
        st.success("No issues found — all business rules passed.")
    for issue in sorted(issues, key=lambda x: x["severity"]):
        icon = {"ERROR": "❌", "WARNING": "⚠️", "INFO": "ℹ️"}.get(issue["severity"], "•")  # noqa: RUF001 - UI icon
        field = f" (`{issue['field']}`)" if issue.get("field") else ""
        st.markdown(f"- {icon} **{issue['code']}**{field}: {issue['message']}")

with st.expander("🧠 Raw LLM extraction (audited payload)"):
    st.json(invoice.get("raw_extraction") or {}, expanded=False)
    st.download_button(
        "Download extraction JSON",
        data=json.dumps(invoice.get("raw_extraction") or {}, indent=2),
        file_name=f"{invoice['number']}.extraction.json",
        mime="application/json",
    )

if st.button("← Back to invoices"):
    st.switch_page("pages/invoices.py")
