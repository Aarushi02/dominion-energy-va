# ------------------------------------------------------------------
# Streamlit app: Dominion Energy Account Profile PDF -> Monthly
# History Excel export.
#
# Upload the PDF, get back a single .xlsx download containing ONLY
# the "Monthly Detail" sheets (one per year found in the document) --
# no Account Profile / Annual Summary sheets, per user request.
#
# Reuses the extraction logic from dominion_pdf_to_spreadsheet.py so
# the parsing behavior (position-aware OCR, unmatched-label warnings,
# PDF-order columns, etc.) stays in one place.
# ------------------------------------------------------------------

import io
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
import pytesseract
from pdf2image import convert_from_path

from dominion_pdf_to_spreadsheet import (
    extract_monthly_tables,
    pivot_monthly_wide_by_year,
)

st.set_page_config(page_title="Dominion Bill -> Monthly History Excel", page_icon="⚡")

st.title("⚡ Dominion Account Profile -> Monthly History")
st.write(
    "Upload a Dominion Energy **Electric Account Profile** PDF. "
    "You'll get back an Excel file containing just the monthly "
    "usage/billing history tables (one sheet per year found in the PDF)."
)

uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])

if uploaded_file is not None:
    with st.status("Processing PDF...", expanded=True) as status:

        # pdf2image needs a real file path, so persist the upload to a
        # temp file first.
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(uploaded_file.read())
            tmp_path = tmp.name

        st.write("Running OCR on each page...")
        images = convert_from_path(tmp_path, dpi=300)
        page_texts = [
            pytesseract.image_to_string(img, config="--psm 6") for img in images
        ]

        st.write("Extracting monthly history tables...")
        monthly_long_df = extract_monthly_tables(images, page_texts)

        if monthly_long_df.empty:
            status.update(label="No monthly history tables found", state="error")
            st.error(
                "No 'Historical Electricity Usage' tables were found in this PDF. "
                "Make sure you uploaded a Dominion Electric Account Profile report."
            )
        else:
            monthly_wide_by_year = pivot_monthly_wide_by_year(monthly_long_df)
            years_found = sorted(monthly_wide_by_year.keys())

            st.write(f"Found monthly history for: {', '.join(years_found)}")

            # ---------------- Build the Excel file in memory ----------------
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                for year in years_found:
                    monthly_wide_by_year[year].to_excel(
                        writer, sheet_name=f"Monthly_Detail_{year}", index=False
                    )
            buffer.seek(0)

            status.update(label="Done", state="complete")

            st.success(f"Extracted {len(years_found)} year(s) of monthly history.")

            # Preview each year's table in the app
            for year in years_found:
                with st.expander(f"Preview: {year}"):
                    st.dataframe(monthly_wide_by_year[year], use_container_width=True)

            out_filename = Path(uploaded_file.name).stem + "_monthly_history.xlsx"
            st.download_button(
                label="⬇️ Download Monthly History Excel",
                data=buffer,
                file_name=out_filename,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        # clean up temp file
        Path(tmp_path).unlink(missing_ok=True)
else:
    st.info("Upload a PDF to get started.")
