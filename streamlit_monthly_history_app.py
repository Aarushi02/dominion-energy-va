# ------------------------------------------------------------------
# Streamlit app: Dominion Bill Processing
#
# Two sections, selected via sidebar:
#   1. Bill Upload      -- PDF -> Monthly History Excel (unchanged
#                           conversion logic from before)
#   2. Audit Calculation -- upload an already-converted Monthly History
#                           Excel (from section 1, or a prior session)
#                           and run it against a PRE-BUNDLED tariff
#                           logic JSON (no upload needed each time --
#                           see TARIFF_JSON_PATH below).
#
# The tariff JSON must be committed to the repo alongside this file.
# Update TARIFF_JSON_PATH if you rename it or add versioned tariffs.
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
from audit_integration import run_audit, read_monthly_detail_spreadsheet

st.set_page_config(page_title="Dominion Bill Processing", page_icon="⚡")

# Path to the pre-bundled tariff logic JSON. Commit this file to the
# repo -- e.g. `final_tariff_logic.json` from a dominion_tariff_pipeline_v2.py
# run -- so the audit section never needs a per-session upload.
TARIFF_JSON_PATH = "final_tariff_logic.json"


# ============================================================
# AUTH -- unchanged from before
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

with st.sidebar:
    if st.button("Log out"):
        st.session_state["authenticated"] = False
        st.rerun()

    st.divider()
    section = st.radio("Section", ["📄 Bill Upload", "🔍 Audit Calculation"])


# ============================================================
# Cache the audit engine so the tariff JSON isn't re-parsed on
# every rerun -- only reloads if the file's path/mtime changes.
# ============================================================

@st.cache_resource
def load_audit_engine(path: str):
    return DominionAuditEngine(path)


# ============================================================
# SECTION 1: Bill Upload (PDF -> Monthly History Excel)
# ============================================================

def render_bill_upload():
    st.title("⚡ Dominion Account Profile -> Monthly History")
    st.write(
        "Upload a Dominion Energy **Electric Account Profile** PDF. "
        "You'll get back an Excel file containing the monthly "
        "usage/billing history tables (one sheet per year found in the PDF)."
    )

    uploaded_file = st.file_uploader("Upload PDF", type=["pdf"])

    if uploaded_file is None:
        st.info("Upload a PDF to get started.")
        return

    with st.status("Processing PDF...", expanded=True) as status:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            tmp.write(uploaded_file.read())
            tmp_path = tmp.name

        st.write("Running OCR on each page...")
        images = convert_from_path(tmp_path, dpi=300)
        page_texts = [pytesseract.image_to_string(img, config="--psm 6") for img in images]

        st.write("Extracting monthly history tables...")
        monthly_long_df = extract_monthly_tables(images, page_texts)

        if monthly_long_df.empty:
            status.update(label="No monthly history tables found", state="error")
            st.error(
                "No 'Historical Electricity Usage' tables were found in this PDF. "
                "Make sure you uploaded a Dominion Electric Account Profile report."
            )
            Path(tmp_path).unlink(missing_ok=True)
            return

        monthly_wide_by_year = pivot_monthly_wide_by_year(monthly_long_df)
        years_found = sorted(monthly_wide_by_year.keys())
        st.write(f"Found monthly history for: {', '.join(years_found)}")

        buffer = io.BytesIO()
        with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
            for year in years_found:
                monthly_wide_by_year[year].to_excel(
                    writer, sheet_name=f"Monthly_Detail_{year}", index=False
                )
        buffer.seek(0)

        status.update(label="Done", state="complete")
        st.success(f"Extracted {len(years_found)} year(s) of monthly history.")

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

        st.info("💡 Go to **Audit Calculation** in the sidebar to run a bill audit "
                "on this spreadsheet.")

        Path(tmp_path).unlink(missing_ok=True)


# ============================================================
# SECTION 2: Audit Calculation (spreadsheet -> audit results)
# ============================================================

def render_audit_calculation():
    st.title("🔍 Bill Audit Calculation")
    st.write(
        "Upload a **Monthly History Excel** (from the Bill Upload section) "
        "to check actual charges against tariff logic."
    )

    if not Path(TARIFF_JSON_PATH).exists():
        st.error(
            f"Tariff logic file not found at `{TARIFF_JSON_PATH}`. "
            f"This file needs to be committed to the repo alongside the app "
            f"(output of dominion_tariff_pipeline_v2.py)."
        )
        return

    engine = load_audit_engine(TARIFF_JSON_PATH)
    st.caption(f"Using tariff logic: `{TARIFF_JSON_PATH}` "
               f"({len(engine.tariff_map)} schedule(s) loaded)")

    spreadsheet_file = st.file_uploader("Upload Monthly History Excel", type=["xlsx"])

    if spreadsheet_file is None:
        st.info("Upload a spreadsheet to run the audit.")
        return

    with st.status("Running audit...", expanded=True) as status:
        st.write("Reading monthly history spreadsheet...")
        monthly_long_df = read_monthly_detail_spreadsheet(spreadsheet_file)

        if monthly_long_df.empty:
            status.update(label="No monthly data found", state="error")
            st.error(
                "No 'Monthly_Detail_<year>' sheets found in this file. "
                "Make sure you uploaded the Excel file from the Bill Upload section."
            )
            return

        st.write("Calculating expected charges per month...")
        audit_results_df = run_audit(monthly_long_df, engine)

        status.update(label="Done", state="complete")

    if audit_results_df.empty:
        st.warning("No auditable months found (missing usage or rate data).")
        return

    skipped = (audit_results_df["status"] == "SKIPPED").sum()
    if skipped:
        st.info(f"{skipped} month(s) skipped -- no tariff logic found "
                f"for that schedule in `{TARIFF_JSON_PATH}`.")

    def _highlight_variance(row):
        if row["status"] != "SUCCESS":
            return [""] * len(row)
        threshold = max(10.0, 0.05 * abs(row["actual_bill"]))
        color = "background-color: #ffcccc" if abs(row["variance"]) > threshold else ""
        return [color] * len(row)

    st.dataframe(
        audit_results_df.style.apply(_highlight_variance, axis=1),
        use_container_width=True,
    )

    out_buffer = io.BytesIO()
    with pd.ExcelWriter(out_buffer, engine="openpyxl") as writer:
        audit_results_df.to_excel(writer, sheet_name="Audit_Results", index=False)
    out_buffer.seek(0)

    out_filename = Path(spreadsheet_file.name).stem + "_audit_results.xlsx"
    st.download_button(
        label="⬇️ Download Audit Results Excel",
        data=out_buffer,
        file_name=out_filename,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ============================================================
# ROUTE
# ============================================================

if section == "📄 Bill Upload":
    render_bill_upload()
else:
    render_audit_calculation()
