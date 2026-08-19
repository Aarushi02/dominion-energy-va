# Streamlit app: Dominion Bill Processing

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
from audit_integration import run_audit, read_monthly_detail_spreadsheet, compare_schedules

st.set_page_config(page_title="Dominion Bill Processing", page_icon="⚡")

TARIFF_JSON_PATH = "final_tariff_logic.json"

SURCHARGE_JSON_PATH = "surcharge_rates.json"

# AUTHENTICATION

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
    section = st.radio("Section", ["📄 Bill Upload", "🔍 Audit Calculation", "📋 Tariff Viewer", "💰 Sales & Use Tax Surcharge"])

@st.cache_resource
def load_audit_engine(path: str):
    return DominionAuditEngine(path)

# SECTION 1: Bill Upload (PDF -> Monthly History Excel)

@st.cache_data(show_spinner=False)
def process_pdf(file_bytes: bytes):
    
    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        tmp.write(file_bytes)
        tmp_path = tmp.name

    try:
        images = convert_from_path(tmp_path, dpi=300)
        page_texts = [pytesseract.image_to_string(img, config="--psm 6") for img in images]
        monthly_long_df = extract_monthly_tables(images, page_texts)
    finally:
        Path(tmp_path).unlink(missing_ok=True)

    return monthly_long_df


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

    file_bytes = uploaded_file.read()

    with st.spinner("Processing PDF..."):
        monthly_long_df = process_pdf(file_bytes)

    if monthly_long_df.empty:
        st.error(
            "No 'Historical Electricity Usage' tables were found in this PDF. "
            "Make sure you uploaded a Dominion Electric Account Profile report."
        )
        return

    monthly_wide_by_year = pivot_monthly_wide_by_year(monthly_long_df)
    years_found = sorted(monthly_wide_by_year.keys())
    st.success(f"Extracted {len(years_found)} year(s) of monthly history: {', '.join(years_found)}")

    buffer = io.BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for year in years_found:
            monthly_wide_by_year[year].to_excel(
                writer, sheet_name=f"Monthly_Detail_{year}", index=False
            )
    buffer.seek(0)

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

# SECTION 2: Audit Calculation

@st.cache_data(show_spinner=False)
def process_audit(file_bytes: bytes, tariff_json_path: str):

    engine = load_audit_engine(tariff_json_path)
    monthly_long_df = read_monthly_detail_spreadsheet(io.BytesIO(file_bytes))

    if monthly_long_df.empty:
        return monthly_long_df, pd.DataFrame()

    audit_results_df = run_audit(monthly_long_df, engine)
    return monthly_long_df, audit_results_df


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

    file_bytes = spreadsheet_file.read()

    with st.spinner("Running audit (this only happens once per file)..."):
        monthly_long_df, audit_results_df = process_audit(file_bytes, TARIFF_JSON_PATH)

    if monthly_long_df.empty:
        st.error(
            "No 'Monthly_Detail_<year>' sheets found in this file. "
            "Make sure you uploaded the Excel file from the Bill Upload section."
        )
        return

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

    st.subheader("Audit Results (as actually billed)")
    st.dataframe(
        audit_results_df.style.apply(_highlight_variance, axis=1),
        use_container_width=True,
    )

    out_buffer = io.BytesIO()
    with pd.ExcelWriter(out_buffer, engine="openpyxl") as writer:
        audit_results_df.to_excel(writer, sheet_name="Audit_Results", index=False)
    out_buffer.seek(0)

    st.download_button(
        label="⬇️ Download Audit Results (.xlsx)",
        data=out_buffer,
        file_name=Path(spreadsheet_file.name).stem + "_audit_results.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    #Schedule comparison
    st.divider()
    st.subheader("Compare Against Other Schedule(s)")
    st.write(
        "See what the same actual monthly usage would have cost under a "
        "different rate schedule."
    )

    billed_schedules = sorted(audit_results_df["schedule"].dropna().unique())
    all_schedules = sorted(engine.tariff_map.keys())
    comparison_options = [s for s in all_schedules if s not in billed_schedules]

    selected_schedules = st.multiselect(
        "Select schedule(s) to compare against",
        options=comparison_options,
        default=[],
    )

    if selected_schedules:
        compare_targets = billed_schedules + selected_schedules
        with st.spinner("Calculating comparison..."):
            comparison_df = compare_schedules(monthly_long_df, engine, compare_targets)

        if comparison_df.empty:
            st.warning("Comparison could not be calculated for this data.")
        else:
            st.dataframe(comparison_df, use_container_width=True)

            comp_buffer = io.BytesIO()
            with pd.ExcelWriter(comp_buffer, engine="openpyxl") as writer:
                comparison_df.to_excel(writer, sheet_name="Schedule_Comparison", index=False)
            comp_buffer.seek(0)
            st.download_button(
                label="⬇️ Download Schedule Comparison (.xlsx)",
                data=comp_buffer,
                file_name=Path(spreadsheet_file.name).stem + "_schedule_comparison.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
    else:
        st.caption("Select one or more schedules above to run the comparison.")

# SECTION 3: Tariff Viewer 

def _billing_model_badge(billing_model):
    labels = {
        "usage_based": "⚡ Usage-based (kWh/kW)",
        "per_fixture": "💡 Per-fixture (equipment count)",
        "flat_service_fee": "🔧 Flat service fee",
        "variable_pricing": "📈 Variable/market pricing",
        "not_ratable": "📄 Not independently billable",
    }
    return labels.get(billing_model, billing_model or "Unclassified")


def _render_charge_blocks(charge_blocks):

    if not charge_blocks:
        st.caption("No charge blocks extracted for this entry.")
        return

    seen_names = []
    grouped = {}
    for b in charge_blocks:
        name = b.get("block_name", "Charge")
        if name not in grouped:
            grouped[name] = []
            seen_names.append(name)
        grouped[name].append(b)

    for name in seen_names:
        st.markdown(f"*{name}*")
        rows = []
        for b in grouped[name]:
            condition = b.get("condition") or "—"
            basis = b.get("basis", "")
            unit = b.get("unit", "")
            rs = b.get("rate_structure", {}) or {}

            if rs.get("type") == "tiered":
                for i, tier in enumerate(rs.get("tiers", []), start=1):
                    threshold = tier.get("threshold")
                    tbasis = tier.get("threshold_basis", "flat")
                    if threshold is None:
                        tier_label = "Additional (open-ended)"
                    elif tbasis == "flat":
                        tier_label = f"Tier {i}: up to {threshold:,.0f}"
                    else:
                        tier_label = f"Tier {i}: up to {threshold:,.0f} × demand ({tbasis})"
                    rows.append({
                        "Condition": condition,
                        "Basis": basis,
                        "Tier": tier_label,
                        "Rate": tier.get("rate"),
                        "Unit": unit,
                    })
            elif rs.get("type") == "flat":
                rows.append({
                    "Condition": condition,
                    "Basis": basis,
                    "Tier": "—",
                    "Rate": rs.get("rate"),
                    "Unit": unit,
                })

        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)


def _render_fixture_rates(fixture_rates):
    if not fixture_rates:
        st.caption("No fixture rate table extracted for this entry.")
        return
    df = pd.DataFrame(fixture_rates)
    display_cols = [c for c in ["fixture_label", "distribution_charge_per_unit",
                                  "supply_charge_per_unit", "period"] if c in df.columns]
    st.dataframe(df[display_cols], use_container_width=True, hide_index=True)


def render_tariff_viewer():
    st.title("📋 Tariff Viewer")
    st.write("Browse the rates loaded from the tariff logic file.")

    if not Path(TARIFF_JSON_PATH).exists():
        st.error(
            f"Tariff logic file not found at `{TARIFF_JSON_PATH}`. "
            f"This file needs to be committed to the repo alongside the app "
            f"(output of dominion_tariff_pipeline_v2.py)."
        )
        return

    engine = load_audit_engine(TARIFF_JSON_PATH)
    st.caption(f"Using tariff logic: `{TARIFF_JSON_PATH}`")

    schedule_codes = sorted(engine.tariff_map.keys())
    if not schedule_codes:
        st.warning("No schedules found in the tariff logic file.")
        return

    selected_schedule = st.selectbox("Select a schedule", schedule_codes)

    entries = engine.tariff_map.get(selected_schedule, [])
    if not entries:
        st.info("No rate data found for this schedule.")
        return

    for entry in entries:
        description = entry.get("description") or "General"
        eff_date = entry.get("_effective_date")
        billing_model = entry.get("billing_model")

        header = f"{description}" + (f" (effective {eff_date})" if eff_date else "")
        with st.expander(header, expanded=True):
            st.caption(_billing_model_badge(billing_model))

            logic_steps = entry.get("logic_steps", [])
            riders = entry.get("riders_priced", [])
            charge_blocks = entry.get("charge_blocks", [])
            fixture_rates = entry.get("fixture_rates", [])

            fixed_fee_steps = [s for s in logic_steps if (s.get("charge_type") or "") == "fixed_fee"]
            if fixed_fee_steps:
                st.markdown("**Fixed Charges**")
                steps_df = pd.DataFrame(fixed_fee_steps)
                display_cols = [c for c in ["step_name", "value", "period"] if c in steps_df.columns]
                st.dataframe(steps_df[display_cols], use_container_width=True, hide_index=True)

            if billing_model == "usage_based":
                st.markdown("**Usage-Based Charges**")
                _render_charge_blocks(charge_blocks)

            elif billing_model == "per_fixture":
                st.markdown("**Fixture Rate Table**")
                _render_fixture_rates(fixture_rates)

            other_steps = [s for s in logic_steps if (s.get("charge_type") or "") != "fixed_fee"]
            if other_steps:
                st.markdown("**Other Charges**")
                other_df = pd.DataFrame(other_steps)
                display_cols = [c for c in other_df.columns if c not in ("charge_type",)]
                st.dataframe(other_df[display_cols], use_container_width=True, hide_index=True)

            min_charge = entry.get("minimum_charge")
            if min_charge and (min_charge.get("value") is not None or min_charge.get("basis")):
                if min_charge.get("value") is not None:
                    st.caption(f"💵 Minimum charge: ${min_charge['value']:.2f} ({min_charge.get('basis', '')})")
                else:
                    st.caption(f"💵 Minimum charge: {min_charge.get('basis', 'see tariff text')} "
                               f"(not a fixed number -- not auto-enforced)")

            demand_note = entry.get("demand_determination_note")
            if demand_note:
                st.caption(f"📏 Demand determination: {demand_note}")

            if entry.get("notes"):
                st.caption(f"ℹ️ {entry['notes']}")
            if entry.get("riders_note"):
                st.caption(f"ℹ️ {entry['riders_note']}")

            st.markdown("**Riders**")
            if riders:
                riders_df = pd.DataFrame(riders)
                display_cols = [c for c in ["rider_name", "value", "unit", "withdrawn_date"] if c in riders_df.columns]
                st.dataframe(riders_df[display_cols], use_container_width=True, hide_index=True)

                total_kwh_riders = sum(r["value"] for r in riders if r.get("unit") == "kwh")
                total_kw_riders = sum(r["value"] for r in riders if r.get("unit") == "kw")
                withdrawn_count = sum(1 for r in riders if r.get("withdrawn_date"))
                st.caption(
                    f"Rider totals: **${total_kwh_riders:.5f}/kWh** + **${total_kw_riders:.5f}/kW** "
                    f"across {len(riders)} rider(s)"
                    + (f" ({withdrawn_count} withdrawn as of a listed date)" if withdrawn_count else "")
                )
            else:
                st.caption("No priced riders found for this schedule.")

            source_pages = entry.get("source_pages")
            if source_pages:
                st.caption(f"Source: pages {source_pages} of the tariff document")

# SECTION 4: Sales & Use Tax Surcharge

@st.cache_data(show_spinner=False)
def load_surcharge_rates(path: str):
    import json
    with open(path, "r") as f:
        return json.load(f)


def render_surcharge_viewer():
    st.title("💰 Sales & Use Tax Surcharge")
    st.write(
        "Virginia Jurisdiction Sales and Use Tax Surcharge rates, per kWh. "
        "This is a separate rate table from the VEPGA municipal/county tariff "
        "-- schedule codes here (e.g. GS-1, MBR, SCR) use a different naming "
        "convention and don't correspond 1:1 with the VEPGA schedule codes "
        "in the Tariff Viewer section."
    )

    if not Path(SURCHARGE_JSON_PATH).exists():
        st.error(
            f"Surcharge rate file not found at `{SURCHARGE_JSON_PATH}`. "
            f"This file needs to be committed to the repo alongside the app."
        )
        return

    data = load_surcharge_rates(SURCHARGE_JSON_PATH)
    rates = data.get("rates", [])
    if not rates:
        st.warning("No surcharge rates found in the file.")
        return

    source = data.get("source")
    eff_date = data.get("effective_date")
    caption_parts = []
    if source:
        caption_parts.append(source)
    if eff_date:
        caption_parts.append(f"effective on and after {eff_date}")
    if caption_parts:
        st.caption(" \u2014 ".join(caption_parts))

    rows = []
    for e in rates:
        for code in e.get("schedule_codes", []):
            rows.append({
                "Schedule": code,
                "Condition": e.get("condition") or "\u2014",
                "Rate ($/kWh)": e.get("rate_per_kwh"),
            })
    df = pd.DataFrame(rows).sort_values("Schedule").reset_index(drop=True)

    search = st.text_input("Search schedule code", "")
    if search:
        df = df[df["Schedule"].str.contains(search, case=False, na=False)]

    st.dataframe(df, use_container_width=True, hide_index=True)
    st.caption(f"{len(df)} schedule/condition row(s) shown.")

# ROUTE

if section == "📄 Bill Upload":
    render_bill_upload()
elif section == "🔍 Audit Calculation":
    render_audit_calculation()
elif section == "📋 Tariff Viewer":
    render_tariff_viewer()
else:
    render_surcharge_viewer()
