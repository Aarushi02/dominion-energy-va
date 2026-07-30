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
from dominion_audit_engine import DominionAuditEngine
from audit_integration import run_audit

st.set_page_config(page_title="Dominion Bill -> Monthly History Excel", page_icon="⚡")


# ============================================================
# AUTH -- simple username/password gate
#
# Credentials live in Streamlit secrets (st.secrets), NOT hardcoded in
# this file, so they aren't checked into git. On Streamlit Community
# Cloud: App settings -> Secrets -> paste:
#
#   [credentials]
#   username = "your-username"
#   password = "your-password"
#
# For local dev, create a .streamlit/secrets.toml file with the same
# content (and add .streamlit/secrets.toml to .gitignore).
# ============================================================

def check_login():
    if st.session_state.get("authenticated"):
        return True

    st.title("🔒 Login required")

    with st.form("login_form"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Log in")

    if submitted:
        try:
            correct_username = st.secrets["credentials"]["username"]
            correct_password = st.secrets["credentials"]["password"]
        except (KeyError, FileNotFoundError):
            st.error(
                "No credentials configured. Set [credentials] username/password "
                "in Streamlit secrets before deploying."
            )
            return False

        if username == correct_username and password == correct_password:
            st.session_state["authenticated"] = True
            st.rerun()
        else:
            st.error("Incorrect username or password.")

    return False


if not check_login():
    st.stop()

# Optional: let a logged-in user log out
with st.sidebar:
    if st.button("Log out"):
        st.session_state["authenticated"] = False
        st.rerun()


st.title("⚡ Dominion Account Profile -> Monthly History")
st.write(
    "Upload a Dominion Energy **Electric Account Profile** PDF. "
    "You'll get back an Excel file containing the monthly "
    "usage/billing history tables (one sheet per year found in the PDF), "
    "plus an automated bill audit if you also provide a tariff logic file."
)

tariff_json = st.file_uploader(
    "Optional: upload tariff logic JSON (from dominion_tariff_pipeline_v2.py) "
    "to enable automated bill auditing",
    type=["json"],
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

            # ---------------- Optional: run bill audit ----------------
            audit_results_df = pd.DataFrame()
            if tariff_json is not None:
                st.write("Running bill audit against uploaded tariff logic...")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".json", mode="wb") as tmp_json:
                    tmp_json.write(tariff_json.read())
                    tariff_json_path = tmp_json.name

                try:
                    engine = DominionAuditEngine(tariff_json_path)
                    audit_results_df = run_audit(monthly_long_df, engine)
                except Exception as e:
                    st.warning(f"Audit could not run: {e}")
                finally:
                    Path(tariff_json_path).unlink(missing_ok=True)

            # ---------------- Build the Excel file in memory ----------------
            buffer = io.BytesIO()
            with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
                for year in years_found:
                    monthly_wide_by_year[year].to_excel(
                        writer, sheet_name=f"Monthly_Detail_{year}", index=False
                    )
                if not audit_results_df.empty:
                    audit_results_df.to_excel(writer, sheet_name="Audit_Results", index=False)
            buffer.seek(0)

            status.update(label="Done", state="complete")

            st.success(f"Extracted {len(years_found)} year(s) of monthly history.")

            # Preview each year's table in the app
            for year in years_found:
                with st.expander(f"Preview: {year}"):
                    st.dataframe(monthly_wide_by_year[year], use_container_width=True)

            # Preview audit results, if run
            if not audit_results_df.empty:
                with st.expander("🔍 Audit Results", expanded=True):
                    skipped = (audit_results_df["status"] == "SKIPPED").sum()
                    if skipped:
                        st.info(f"{skipped} month(s) skipped -- no tariff logic found "
                                f"for that schedule in the uploaded JSON.")

                    def _highlight_variance(row):
                        if row["status"] != "SUCCESS":
                            return [""] * len(row)
                        # flag anything more than $10 or 5% off, whichever is larger
                        threshold = max(10.0, 0.05 * abs(row["actual_bill"]))
                        color = "background-color: #ffcccc" if abs(row["variance"]) > threshold else ""
                        return [color] * len(row)

                    st.dataframe(
                        audit_results_df.style.apply(_highlight_variance, axis=1),
                        use_container_width=True,
                    )
            elif tariff_json is None:
                st.caption("💡 Upload a tariff logic JSON above to also get an automated "
                           "bill audit (expected vs. actual charges per month).")

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
