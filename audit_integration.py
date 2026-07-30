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
