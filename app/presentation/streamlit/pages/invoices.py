"""Invoices page: search/filter/list — data comes exclusively from the API."""

import api_client
import streamlit as st

client = api_client.get_api_client()

st.title("🧾 Invoices")

search = st.text_input("Search (number or supplier)", placeholder="e.g. INV-2026 or ACME")
col_from, col_to = st.columns(2)
date_from = col_from.date_input("Issue date from", value=None)
date_to = col_to.date_input("Issue date to", value=None)

try:
    page_data = client.list_invoices(
        search=search or None,
        date_from=date_from.isoformat() if date_from else None,
        date_to=date_to.isoformat() if date_to else None,
        limit=50,
    )
except api_client.ApiError as exc:
    st.error(str(exc))
    st.stop()

items = page_data.get("items", [])
st.caption(f"{page_data.get('total', 0)} invoice(s) found")

if not items:
    st.info("No invoices match the current filters.")
    st.stop()


import pandas as pd  # noqa: E402  (display-only dependency)

frame = pd.DataFrame(
    [
        {
            "ID": str(item["id"]),
            "Number": item["number"],
            "Supplier": item["supplier_name"],
            "Tax ID": item["supplier_tax_id"],
            "Date": item["issue_date"],
            "Currency": item["currency"],
            "Total": item["total"],
        }
        for item in items
    ]
)

selection = st.dataframe(
    frame,
    use_container_width=True,
    hide_index=True,
    on_select="rerun",
    selection_mode="single-row",
    column_config={
        "Total": st.column_config.NumberColumn(format="%.2f"),
    },
)

rows = selection.get("selection", {}).get("rows", [])
if rows:
    invoice_id = frame.iloc[rows[0]]["ID"]
    st.session_state["selected_invoice_id"] = invoice_id
    if st.button("Open detail", type="primary"):
        st.switch_page("pages/invoice_detail.py")
