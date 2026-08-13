"""
Builds per-month audit input rows directly from the monthly billing
table extracted by dominion_pdf_to_spreadsheet.py's extract_monthly_tables(),
then runs each through DominionAuditEngine.
 
No separate account-profile lookup needed: the monthly table already
carries everything required per month --
  "Total Consumption" -> billed_kwh
  "Demand"             -> billed_demand
  "Billed Rate"         -> which schedule to audit against (e.g. "VE-100")
  "Total Charges"       -> the actual billed amount to compare against
"""
 
import re
from datetime import datetime
import pandas as pd
 
 
def _normalize_billed_rate_to_sc_code(billed_rate: str) -> str:
    """
    'VE-100' -> 'SCHEDULE 100'. Handles the VE-<code> style Dominion
    prints in the "Billed Rate" row of its monthly history table --
    different from the "Current Rate: Schedule 110" style used on the
    Account Profile summary page, so this needs its own normalization,
    not just reusing DominionAuditEngine's _normalize_sc_code as-is.
    """
    if not billed_rate:
        return ""
    m = re.search(r"VE[-\s]?(\S+)", str(billed_rate).upper())
    code = m.group(1) if m else str(billed_rate).upper()
    return f"SCHEDULE {code}"
 
 
def build_audit_rows(monthly_long_df: pd.DataFrame) -> pd.DataFrame:
    """
    Pivots the long-format monthly table into one row per (year, month)
    with the fields DominionAuditEngine.calculate_expected_bill() needs.
    """
    wide = monthly_long_df.pivot_table(
        index=["year", "month"],
        columns="line_item",
        values="value",
        aggfunc="first",
    ).reset_index()
 
    rows = []
    for _, r in wide.iterrows():
        billed_kwh = r.get("Total Consumption")
        billed_demand = r.get("Demand")
        billed_rate = r.get("Billed Rate")
        actual_bill = r.get("Total Charges")
 
        if pd.isna(billed_kwh) or pd.isna(billed_rate):
            continue  # nothing to audit for a blank/missing month
 
        # bill_date: prefer "Bill To" (end of the billing period -- the
        # natural date to check a rider's withdrawal status against),
        # fall back to "Bill From", then to an approximate month-end
        # date if neither made it through extraction. Without this,
        # DominionAuditEngine has no usable date and silently INCLUDES
        # withdrawn riders (confirmed real gap -- this field was never
        # populated at all before this fix).
        bill_date = None
        for date_field in ("Bill To", "Bill From"):
            raw = r.get(date_field)
            if pd.notna(raw):
                parsed = pd.to_datetime(raw, errors="coerce")
                if pd.notna(parsed):
                    bill_date = parsed
                    break
        if bill_date is None:
            # last resort: approximate as the last day of the month
            try:
                bill_date = pd.Period(f"{r['year']}-{r['month']}", freq="M").end_time
            except Exception:
                bill_date = None
 
        rows.append({
            "year": r["year"],
            "month": r["month"],
            "service_class": _normalize_billed_rate_to_sc_code(billed_rate),
            "billed_kwh": float(billed_kwh),
            "billed_demand": float(billed_demand) if pd.notna(billed_demand) else 0.0,
            # NOTE: deliberately NOT setting is_demand here. A nonzero
            # "Demand" reading just means a demand meter recorded some
            # peak kW -- it does NOT mean the account is billed under
            # the schedule's Demand-Billing tier (confirmed bug: was
            # setting is_demand=True for nearly every month just
            # because demand > 0, even for months well under the
            # tariff's actual 10,000 kWh demand-billing threshold).
            # Leaving is_demand unset lets DominionAuditEngine fall
            # back to its own billed_kwh >= 10,000 rule instead.
            "bill_date": bill_date,
            "bill_amount": float(actual_bill) if pd.notna(actual_bill) else 0.0,
        })
 
    return pd.DataFrame(rows)
 
 
def run_audit(monthly_long_df: pd.DataFrame, engine) -> pd.DataFrame:
    """
    Returns a DataFrame: one row per audited month, with expected_bill,
    variance, and a human-readable trace joined into one string (so it
    fits cleanly in a single Excel cell).
    """
    audit_input = build_audit_rows(monthly_long_df)
    if audit_input.empty:
        return pd.DataFrame()
 
    results = []
    for _, row in audit_input.iterrows():
        result = engine.calculate_expected_bill(row)
        results.append({
            "year": row["year"],
            "month": row["month"],
            "schedule": result.get("sc_code", row["service_class"]),
            "billing_type": result.get("billing_type", ""),
            "billed_kwh": row["billed_kwh"],
            "billed_demand": row["billed_demand"],
            "actual_bill": result.get("actual_bill", row["bill_amount"]),
            "expected_bill": result.get("expected_bill", 0.0),
            "variance": result.get("variance", 0.0),
            "status": result.get("status", "UNKNOWN"),
            "trace": " | ".join(result.get("trace", [])),
        })
 
    return pd.DataFrame(results)
 
 
def read_monthly_detail_spreadsheet(file_obj) -> pd.DataFrame:
    """
    Reads a previously-downloaded 'Monthly History Excel' (one
    Monthly_Detail_<year> sheet per year, wide format: rows=line_item,
    columns=JAN..DEC) back into the long format run_audit() expects:
    [year, month, line_item, value].
 
    This is the reverse of pivot_monthly_wide_by_year() in
    dominion_pdf_to_spreadsheet.py -- lets the audit section accept an
    already-converted spreadsheet directly, rather than requiring the
    original PDF to be re-uploaded and re-OCR'd.
    """
    xl = pd.ExcelFile(file_obj)
    all_rows = []
 
    for sheet_name in xl.sheet_names:
        if not sheet_name.startswith("Monthly_Detail_"):
            continue
        year = sheet_name.replace("Monthly_Detail_", "")
 
        wide = xl.parse(sheet_name)
        month_cols = [c for c in wide.columns if c != "line_item"]
 
        melted = wide.melt(
            id_vars=["line_item"],
            value_vars=month_cols,
            var_name="month",
            value_name="value",
        )
        melted["year"] = year
        melted = melted.dropna(subset=["value"])
        all_rows.append(melted[["year", "month", "line_item", "value"]])
 
    if not all_rows:
        return pd.DataFrame(columns=["year", "month", "line_item", "value"])
 
    return pd.concat(all_rows, ignore_index=True)
 
 
def compare_schedules(monthly_long_df: pd.DataFrame, engine, schedule_codes: list) -> pd.DataFrame:
    """
    Runs every month's actual usage through each schedule in
    schedule_codes (via DominionAuditEngine's override_schedule param),
    returning one row per (month, schedule) so you can directly compare
    what the same usage would have cost under different rate schedules
    -- the same comparison pattern as the City of VA Beach Schedule
    100 vs 130 savings analysis workbook, generalized to any schedules
    you want to compare.
    """
    audit_input = build_audit_rows(monthly_long_df)
    if audit_input.empty:
        return pd.DataFrame()
 
    results = []
    for _, row in audit_input.iterrows():
        for sc in schedule_codes:
            result = engine.calculate_expected_bill(row, override_schedule=sc)
            results.append({
                "year": row["year"],
                "month": row["month"],
                "billed_schedule": result.get("billed_schedule", row["service_class"]),
                "compared_schedule": sc,
                "billed_kwh": row["billed_kwh"],
                "billed_demand": row["billed_demand"],
                "actual_bill": row["bill_amount"],
                "calculated_amount": result.get("expected_bill", 0.0),
                "status": result.get("status", "UNKNOWN"),
            })
 
    df = pd.DataFrame(results)
    if df.empty:
        return df
 
    # pivot wide: one column per compared schedule's calculated_amount,
    # so savings between schedules are directly visible side by side
    wide = df.pivot_table(
        index=["year", "month", "billed_schedule", "billed_kwh", "billed_demand", "actual_bill"],
        columns="compared_schedule",
        values="calculated_amount",
        aggfunc="first",
    ).reset_index()
 
    return wide
 
 
# ---------------------------------------------------------------------
# Text report generation, adapted from the National Grid
# BillAuditReporter._format_text_report pattern to our schema (audit
# results are keyed by year/month/schedule with a trace list, rather
# than sc_code/bill_date/bill_account) and our own high-variance
# definition (max($10, 5% of actual_bill), matching the same threshold
# already used for the in-app red-highlighting, rather than their
# literal >$0.05 check).
# ---------------------------------------------------------------------
 
def _is_high_variance(row) -> bool:
    if row.get("status") != "SUCCESS":
        return False
    threshold = max(10.0, 0.05 * abs(row.get("actual_bill", 0) or 0))
    return abs(row.get("variance", 0) or 0) > threshold
 
 
def format_audit_text_report(results_df: pd.DataFrame, account_label: str = "") -> str:
    """
    Formats run_audit()'s output DataFrame into a text report: a
    SUMMARY block (total variance, high-variance bill count, error/
    skip count) followed by DETAILED RESULTS (one section per month,
    including its full calculation trace) -- mirrors the structure of
    BillAuditReporter._format_text_report, adapted to our audit
    result schema.
    """
    if results_df is None or results_df.empty:
        return "No audit results generated." + (f" (account: {account_label})" if account_label else "")
 
    lines = []
    lines.append("=" * 80)
    lines.append("BILL AUDIT REPORT")
    lines.append("=" * 80)
    if account_label:
        lines.append(f"Account: {account_label}")
    lines.append(f"Generated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append(f"Total Bills Audited: {len(results_df)}")
    lines.append("=" * 80)
    lines.append("")
 
    # ---- Summary ----
    success_rows = results_df[results_df["status"] == "SUCCESS"]
    total_variance = success_rows["variance"].sum() if not success_rows.empty else 0.0
    high_variance_count = sum(_is_high_variance(r) for _, r in results_df.iterrows())
    skipped_count = (results_df["status"] == "SKIPPED").sum()
    partial_count = (results_df["status"] == "PARTIAL").sum() if "PARTIAL" in results_df["status"].values else 0
 
    lines.append("SUMMARY:")
    lines.append(f"  Total Variance (actual - expected, SUCCESS bills only): ${total_variance:.2f}")
    lines.append(f"  High Variance Bills (over $10 or 5%, whichever is greater): {high_variance_count}")
    lines.append(f"  Skipped Bills (no matching tariff logic): {skipped_count}")
    if partial_count:
        lines.append(f"  Partial Bills (non-standard schedule, fixed-fee-only calculation): {partial_count}")
    lines.append("")
    lines.append("-" * 80)
    lines.append("")
 
    # ---- Detailed results ----
    lines.append("DETAILED RESULTS:")
    lines.append("")
 
    for idx, row in results_df.reset_index(drop=True).iterrows():
        lines.append(f"Bill #{idx + 1}: {row.get('year')} {row.get('month')}")
        lines.append(f"  Schedule: {row.get('schedule', 'N/A')}")
        if row.get("billing_type"):
            lines.append(f"  Billing Type: {row.get('billing_type')}")
        lines.append(f"  Usage: {row.get('billed_kwh', 0):,.0f} kWh, {row.get('billed_demand', 0):,.1f} kW demand")
        lines.append(f"  Actual Amount: ${row.get('actual_bill', 0):.2f}")
        lines.append(f"  Expected Amount: ${row.get('expected_bill', 0):.2f}")
        lines.append(f"  Variance: ${row.get('variance', 0):.2f}"
                     + ("  [HIGH VARIANCE]" if _is_high_variance(row) else ""))
        lines.append(f"  Status: {row.get('status', 'UNKNOWN')}")
 
        trace = row.get("trace")
        if trace:
            lines.append("  Calculation Trace:")
            # trace is stored as a single " | "-joined string in
            # run_audit()'s output -- split back into individual lines
            trace_items = trace.split(" | ") if isinstance(trace, str) else trace
            for item in trace_items:
                lines.append(f"    - {item}")
 
        lines.append("")
 
    lines.append("=" * 80)
    lines.append("END OF REPORT")
    lines.append("=" * 80)
 
    return "\n".join(lines)
