# Dominion/VEPGA Audit Engine
import json
import logging
from datetime import datetime, date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Normalization helpers
def _normalize_sc_code(sc: Optional[str]) -> str:
    
    if sc is None:
        return ""
    s = str(sc).upper().replace("-", " ")
    s = " ".join(s.split())  
    if not s.startswith("SCHEDULE"):
        s = f"SCHEDULE {s}"
    return s


def _parse_effective_date(raw: Optional[str]) -> Optional[date]:
    if not raw:
        return None
    try:
        return pd.to_datetime(raw).date()
    except Exception:
        return None


def _extract_effective_date(item: dict) -> Optional[date]:
    eff = (
        item.get("_effective_date")
        or item.get("effective_date")
        or (item.get("metadata") or {}).get("effective_date")
    )
    return _parse_effective_date(eff)

# Billing-type (Non-Demand vs Demand) selection

def _classify_billing_type(description: Optional[str]) -> str:
    
    if not description:
        return "unknown"
    d = description.upper()
    if "NON-DEMAND" in d or "NON DEMAND" in d:
        return "non_demand"
    if "DEMAND" in d:
        return "demand"
    return "unknown"


def _select_billing_type_for_row(row: pd.Series) -> str:

    if "is_demand" in row and pd.notna(row["is_demand"]):
        return "demand" if bool(row["is_demand"]) else "non_demand"

    kwh = float(row.get("billed_kwh", 0) or 0)
    return "demand" if kwh >= 10000 else "non_demand"


def _is_metered_variant(step_name: str) -> Optional[bool]:
    """
    Returns True/False if the step name marks a variant, None if it's not a metered/unmetered
    variant at all (i.e. a normal, unconditional step).
    """
    name = step_name.lower()
    if "unmetered" in name:
        return False
    if "metered" in name:  
        return True
    return None

SUMMER_MONTHS = {6, 7, 8, 9}


def _is_summer_bill(bill_date) -> Optional[bool]:

    if bill_date is None:
        return None
    if isinstance(bill_date, pd.Timestamp) and pd.isna(bill_date):
        return None  # NaT.month is nan, not an error -- must check explicitly
    month = bill_date.month if hasattr(bill_date, "month") else None
    if month is None or (isinstance(month, float) and pd.isna(month)):
        return None
    return int(month) in SUMMER_MONTHS


def _seasonal_variant(step_name: str) -> Optional[str]:

    name = step_name.lower()
    if "(summer)" in name:
        return "summer"
    if "(base)" in name or "(winter)" in name:
        return "base"
    return None

# Tiered ES charge bucketing (150 kWh per kW per tier)
TIER_BUCKET_KWH_PER_KW = 150


def _is_tiered_step(step_name: str) -> bool:
    name = step_name.lower()
    return any(kw in name for kw in ("first 150", "next 150", "additional"))


def _compute_tiered_es_charge(tier_steps: List[dict], billed_kwh: float, billed_demand: float) -> tuple:
    """
    Applies the demand-bucketed tier algorithm
    Returns (total_cost, trace_lines).
    """
    if billed_demand <= 0:
        logger.warning("Tiered ES charge requested but billed_demand is 0 -- "
                        "all usage will fall into the open-ended 'Additional' tier "
                        "since no bucket capacity exists without demand.")

    remaining = billed_kwh
    total = 0.0
    trace = []

    for step in tier_steps:
        name = step.get("step_name", "")
        rate = float(step.get("value", 0) or 0)
        is_last = "additional" in name.lower()

        if is_last:
            take = max(remaining, 0)
        else:
            bucket_capacity = TIER_BUCKET_KWH_PER_KW * billed_demand
            take = min(max(remaining, 0), bucket_capacity)

        cost = take * rate
        total += cost
        remaining -= take
        trace.append(f"{name}: {take:.2f} kWh @ ${rate:.5f} = ${cost:.2f}")

        if remaining <= 0 and not is_last:
            break

    return total, trace

# Demand-threshold calculation method (SCHEDULE 130-style)

DEMAND_THRESHOLD_PARAM_KEYS = [
    "basic_customer_charge",       # BSC, fixed fee
    "supply_kw_rate",              # $ per kW of Supply Demand
    "kw_adj_first_700",            # $ per kW, first 700 kW of Distribution Demand (confirmed negative = credit)
    "kw_adj_over_700",             # $ per kW, Distribution Demand over 700 kW
    "dist_kw_first_700",           # $ per kW, first 700 kW of Distribution Demand
    "dist_kw_over_700",            # $ per kW, Distribution Demand over 700 kW
    "energy_tier_1_rate",          # $ per kWh, first N kWh (tier_1_kwh_cap)
    "energy_tier_2_rate",          # $ per kWh, next N kWh (tier_2_kwh_cap)
    "energy_tier_3_rate",          # $ per kWh, all additional kWh
    "tier_1_kwh_cap",              # flat kWh breakpoint - NOT demand-scaled
    "tier_2_kwh_cap",              # flat kWh breakpoint - NOT demand-scaled
    "riders_per_kw",               # blended rider total, $ per kW of Supply Demand
    "riders_per_kwh",              # blended rider total, $ per kWh
]


def calculate_demand_threshold_bill(params: dict, supply_demand: float,
                                     dist_demand: float, billed_kwh: float) -> tuple:

    p = {k: float(params.get(k, 0) or 0) for k in DEMAND_THRESHOLD_PARAM_KEYS}

    dist_demand_first_700 = min(dist_demand, 700)
    dist_demand_over_700 = max(dist_demand - 700, 0)

    tier_1 = min(billed_kwh, p["tier_1_kwh_cap"]) if p["tier_1_kwh_cap"] else billed_kwh
    remaining_after_tier1 = max(billed_kwh - tier_1, 0)
    tier_2 = min(remaining_after_tier1, p["tier_2_kwh_cap"]) if p["tier_2_kwh_cap"] else remaining_after_tier1
    tier_3 = max(billed_kwh - tier_1 - tier_2, 0)

    bsc = p["basic_customer_charge"]
    supply_kw_cost = p["supply_kw_rate"] * supply_demand
    kw_adj_cost = (p["kw_adj_first_700"] * dist_demand_first_700
                   + p["kw_adj_over_700"] * dist_demand_over_700)
    dist_kw_cost = (p["dist_kw_first_700"] * dist_demand_first_700
                    + p["dist_kw_over_700"] * dist_demand_over_700)
    energy_cost = (p["energy_tier_1_rate"] * tier_1
                   + p["energy_tier_2_rate"] * tier_2
                   + p["energy_tier_3_rate"] * tier_3)
    rider_cost = p["riders_per_kw"] * supply_demand + p["riders_per_kwh"] * billed_kwh

    total = bsc + supply_kw_cost + kw_adj_cost + dist_kw_cost + energy_cost + rider_cost

    trace = [
        f"Basic Customer Charge: ${bsc:.2f}",
        f"Supply KW ({supply_demand:.2f} kW @ ${p['supply_kw_rate']:.5f}): ${supply_kw_cost:.2f}",
        f"KW Adjustment (700 kW threshold): ${kw_adj_cost:.2f}",
        f"Distribution KW (700 kW threshold): ${dist_kw_cost:.2f}",
        f"Energy tiers ({tier_1:.0f}/{tier_2:.0f}/{tier_3:.0f} kWh): ${energy_cost:.2f}",
        f"Riders ({supply_demand:.2f} kW + {billed_kwh:.0f} kWh): ${rider_cost:.2f}",
    ]
    return total, trace

def _resolve_basis_quantity(basis: str, condition: Optional[str], row: pd.Series) -> float:
    """
    Maps a charge_block's basis (+ condition, for on/off-peak splits)
    to the actual billed quantity from the row.
    """
    cond = (condition or "").lower()

    if basis == "kwh":
        if "on-peak" in cond or "on peak" in cond:
            return float(row.get("on_peak_kwh", 0) or 0)
        if "off-peak" in cond or "off peak" in cond:
            return float(row.get("off_peak_kwh", 0) or 0)
        return float(row.get("billed_kwh", 0) or 0)

    if basis == "kw_distribution_demand":
        return float(row.get("dist_demand", row.get("billed_demand", 0)) or 0)

    if basis == "kw_es_demand":
        if "on-peak" in cond or "on peak" in cond:
            return float(row.get("on_peak_demand", row.get("supply_demand", row.get("billed_demand", 0))) or 0)
        return float(row.get("supply_demand", row.get("billed_demand", 0)) or 0)

    if basis == "kw_contract_demand":
        return float(row.get("contract_demand", row.get("billed_demand", 0)) or 0)

    if basis == "rkva":
        return float(row.get("billed_rkva", 0) or 0)

    if basis == "kw":
        logger.warning("Charge block basis is generic 'kw' (not a valid enum value) -- "
                        "treating as kw_es_demand. Consider fixing the source tariff JSON.")
        return float(row.get("supply_demand", row.get("billed_demand", 0)) or 0)

    return 0.0


def _condition_matches(condition: Optional[str], row: pd.Series, bill_date) -> bool:
    """
    Gates voltage- and season-conditioned charge_blocks
    """
    if condition is None:
        return True
    c = condition.lower()

    if "primary voltage" in c:
        return str(row.get("voltage_class", "secondary")).lower() == "primary"
    if "secondary voltage" in c:
        return str(row.get("voltage_class", "secondary")).lower() != "primary"

    if "summer" in c:
        is_summer = _is_summer_bill(bill_date)
        return bool(is_summer)  # unknown date -> default to NOT summer (matches existing safe-default philosophy)
    if "base" in c or "winter" in c:
        is_summer = _is_summer_bill(bill_date)
        return is_summer is not True  # unknown date -> default TO base/winter

    if "on-peak" in c or "off-peak" in c or "on peak" in c or "off peak" in c:
        return True  

    return True  


def _apply_tiered_rate(tiers: List[dict], quantity: float, es_demand: float, dist_demand: float) -> tuple:

    remaining = quantity
    total = 0.0
    trace = []

    for tier in tiers:
        threshold = tier.get("threshold")
        tbasis = tier.get("threshold_basis", "flat")
        rate = float(tier.get("rate", 0) or 0)

        if threshold is None:
            take = max(remaining, 0)
        else:
            if tbasis == "per_kw_of_es_demand":
                cap = float(threshold) * es_demand
            elif tbasis == "per_kw_of_distribution_demand":
                cap = float(threshold) * dist_demand
            elif tbasis == "per_kw":
                logger.warning("Tier threshold_basis is generic 'per_kw' (not a valid enum "
                                "value) -- treating as per_kw_of_es_demand. Consider fixing "
                                "the source tariff JSON.")
                cap = float(threshold) * es_demand
            else:
                cap = float(threshold)
            take = min(max(remaining, 0), cap)

        cost = take * rate
        total += cost
        remaining -= take
        trace.append(f"    tier ({tbasis}, threshold={threshold}): {take:.2f} @ ${rate:.5f} = ${cost:.2f}")

        if remaining <= 0 and threshold is not None:
            break

    return total, trace


def calculate_charge_blocks_bill(charge_blocks: List[dict], row: pd.Series, bill_date) -> tuple:
    """
    Applies every applicable charge_block (per its condition) to the
    row, returning (total_cost, trace_lines)
    """
    total = 0.0
    trace = []

    dist_demand = float(row.get("dist_demand", row.get("billed_demand", 0)) or 0)
    es_demand = float(row.get("supply_demand", row.get("billed_demand", 0)) or 0)

    for block in charge_blocks:
        condition = block.get("condition")
        if not _condition_matches(condition, row, bill_date):
            continue

        basis = block.get("basis", "")
        quantity = _resolve_basis_quantity(basis, condition, row)
        rs = block.get("rate_structure", {}) or {}
        name = block.get("block_name", "Charge")
        label = f"{name} [{condition}]" if condition else name

        if rs.get("type") == "flat":
            rate = float(rs.get("rate", 0) or 0)
            cost = rate * quantity
            total += cost
            trace.append(f"{label}: {quantity:.2f} @ ${rate:.5f} = ${cost:.2f}")
        elif rs.get("type") == "tiered":
            cost, tier_trace = _apply_tiered_rate(rs.get("tiers", []), quantity, es_demand, dist_demand)
            total += cost
            trace.append(f"{label}:")
            trace.extend(tier_trace)
        else:
            logger.warning(f"Unknown rate_structure type for block '{name}', skipping")
            continue

    return total, trace

# Audit Engine

class DominionAuditEngine:
    
    def __init__(self, tariff_definitions_paths):
        if isinstance(tariff_definitions_paths, str):
            tariff_definitions_paths = [tariff_definitions_paths]
        self.tariff_map = self._load_logic(tariff_definitions_paths)

    # Load tariff logic
   
    def _load_logic(self, paths) -> Dict[str, List[dict]]:
        mapping: Dict[str, List[dict]] = {}
        for path in paths:
            with open(path, "r") as f:
                data = json.load(f)

            if isinstance(data, dict) and "tariffs" in data:
                data = data["tariffs"]

            for item in data:
                sc = _normalize_sc_code(item.get("sc_code") or item.get("source_schedule"))
                item["_effective_date"] = _extract_effective_date(item)
                item["_billing_type"] = _classify_billing_type(item.get("description"))
                mapping.setdefault(sc, []).append(item)

        for sc, items in mapping.items():
            items.sort(key=lambda x: (x["_effective_date"] is None, x["_effective_date"] or date.min))

        return mapping
    def _pick_logic_for_bill(self, sc_code: str, billing_type: str, bill_dt) -> Optional[dict]:
        versions = self.tariff_map.get(sc_code)
        if not versions:
            return None
            
        matching = [v for v in versions if v["_billing_type"] == billing_type]
        if not matching:
            matching = versions  # schedule doesn't branch by billing type

        if not bill_dt:
            return matching[-1]

        if isinstance(bill_dt, datetime):
            bill_dt = bill_dt.date()

        dated = [v for v in matching if v["_effective_date"] and v["_effective_date"] <= bill_dt]
        return max(dated, key=lambda v: v["_effective_date"]) if dated else matching[0]

    # Core calculation

    def calculate_expected_bill(self, row: pd.Series, override_schedule: Optional[str] = None) -> dict:
        
        billed_schedule = _normalize_sc_code(row.get("service_class") or row.get("rate_schedule") or row.get("current_rate"))
        sc_code = _normalize_sc_code(override_schedule) if override_schedule else billed_schedule
        billing_type = _select_billing_type_for_row(row)

        bill_date = row.get("read_date") or row.get("bill_date")
        try:
            bill_date = pd.to_datetime(bill_date)
        except Exception:
            pass

        logic = self._pick_logic_for_bill(sc_code, billing_type, bill_date)
        if not logic:
            return {
                "status": "SKIPPED",
                "expected_bill": 0.0,
                "variance": 0.0,
                "trace": [f"No tariff logic found for {sc_code} ({billing_type})"],
            }

        logic_steps = logic.get("logic_steps", [])
        riders = logic.get("riders_priced", [])

        billed_kwh = float(row.get("billed_kwh", 0) or 0)
        billed_demand = float(row.get("billed_demand", 0) or 0)
        is_metered = bool(row.get("is_metered", True)) 
        
        billing_model = logic.get("billing_model")
        if billing_model and billing_model != "usage_based":
            reason = {
                "per_fixture": "billed per equipment unit, not kWh/kW -- needs fixture counts as input, not supported by this audit path",
                "flat_service_fee": "flat service fee unrelated to kWh/kW usage (e.g. hourly service) -- not auditable via usage data",
                "variable_pricing": "rates are not static figures in the tariff document (e.g. real-time market pricing) -- cannot be calculated here",
                "not_ratable": "this schedule text is definitional/standby/discount-modifier content, not a standalone billable rate",
            }.get(billing_model, f"billing_model '{billing_model}' is not auditable via kWh/kW usage")

            fixed_fee_steps = [s for s in logic_steps if (s.get("charge_type") or "") == "fixed_fee"]
            if fixed_fee_steps:
                trace = [f"Schedule {sc_code}: {reason}",
                         "Computing PARTIAL bill from fixed-fee components only "
                         "(non-standard components below are NOT included):"]
                total_expected = 0.0
                for s in fixed_fee_steps:
                    val = float(s.get("value", 0) or 0)
                    total_expected += val
                    trace.append(f"  {s.get('step_name', 'Fixed Charge')}: ${val:.2f}")

                rider_total = 0.0
                for r in riders:
                    rate = float(r.get("value", 0) or 0)
                    unit = (r.get("unit") or "").lower()
                    if unit == "kwh":
                        rider_total += rate * billed_kwh
                    elif unit == "kw":
                        rider_total += rate * float(row.get("supply_demand", billed_demand) or 0)
                if rider_total:
                    total_expected += rider_total
                    trace.append(f"  Riders: ${rider_total:.2f}")

                actual = float(row.get("bill_amount", 0) or 0)
                return {
                    "status": "PARTIAL",
                    "sc_code": sc_code,
                    "billed_schedule": billed_schedule,
                    "billing_type": billing_type,
                    "actual_bill": round(actual, 2),
                    "expected_bill": round(total_expected, 2),
                    "variance": round(actual - total_expected, 2),
                    "trace": trace,
                }

            return {
                "status": "SKIPPED",
                "sc_code": sc_code,
                "billed_schedule": billed_schedule,
                "expected_bill": 0.0,
                "variance": 0.0,
                "trace": [f"Schedule {sc_code}: {reason}"],
            }


        if billing_model == "usage_based" and "charge_blocks" in logic:
            bill_date_for_blocks = bill_date if isinstance(bill_date, (pd.Timestamp, datetime)) else None
            if bill_date_for_blocks is not None and pd.isna(bill_date_for_blocks):
                bill_date_for_blocks = None

            trace: List[str] = [f"Schedule: {sc_code} ({billing_type}, charge_blocks method)"]
            total_expected = 0.0

            for s in logic_steps:
                variant = _is_metered_variant(s.get("step_name", ""))
                if variant is not None and variant != is_metered:
                    continue
                if (s.get("charge_type") or "") == "fixed_fee":
                    val = float(s.get("value", 0) or 0)
                    total_expected += val
                    trace.append(f"{s.get('step_name', 'Customer Charge')}: ${val:.2f}")

            block_cost, block_trace = calculate_charge_blocks_bill(
                logic.get("charge_blocks", []), row, bill_date_for_blocks
            )
            total_expected += block_cost
            trace.extend(block_trace)

            rider_total = 0.0
            riders_applied = 0
            riders_skipped_withdrawn = []
            for r in riders:
                withdrawn_date = _parse_effective_date(r.get("withdrawn_date"))
                if withdrawn_date and bill_date_for_blocks is not None:
                    bd = bill_date_for_blocks.date() if isinstance(bill_date_for_blocks, datetime) else bill_date_for_blocks
                    if withdrawn_date <= bd:
                        riders_skipped_withdrawn.append(r.get("rider_name"))
                        continue
                rate = float(r.get("value", 0) or 0)
                unit = (r.get("unit") or "").lower()
                if unit == "kwh":
                    rider_total += rate * billed_kwh
                    riders_applied += 1
                elif unit == "kw":
                    rider_total += rate * float(row.get("supply_demand", billed_demand) or 0)
                    riders_applied += 1
            if rider_total:
                total_expected += rider_total
                trace.append(f"Total Riders ({riders_applied} applied): ${rider_total:.2f}")
            if riders_skipped_withdrawn:
                trace.append(f"Riders excluded as withdrawn by bill date: {', '.join(riders_skipped_withdrawn)}")

            min_charge = logic.get("minimum_charge") or {}
            if min_charge.get("value") is not None:
                min_val = float(min_charge["value"])
                if total_expected < min_val:
                    total_expected = min_val
                    trace.append(f"Minimum bill enforced: ${min_val:.2f}")

            actual = float(row.get("bill_amount", 0) or 0)
            return {
                "status": "SUCCESS",
                "sc_code": sc_code,
                "billed_schedule": billed_schedule,
                "billing_type": billing_type,
                "actual_bill": round(actual, 2),
                "expected_bill": round(total_expected, 2),
                "variance": round(actual - total_expected, 2),
                "trace": trace,
            }

        
        if logic.get("calculation_method") == "demand_threshold":
            params = logic.get("params", {})
            supply_demand = float(row.get("supply_demand", billed_demand) or 0)
            dist_demand = float(row.get("dist_demand", billed_demand) or 0)

            total_expected, trace = calculate_demand_threshold_bill(
                params, supply_demand=supply_demand, dist_demand=dist_demand, billed_kwh=billed_kwh
            )
            trace = [f"Schedule: {sc_code} ({billing_type}, demand_threshold method)"] + trace

            actual = float(row.get("bill_amount", 0) or 0)
            return {
                "status": "SUCCESS",
                "sc_code": sc_code,
                "billed_schedule": billed_schedule,
                "billing_type": billing_type,
                "actual_bill": round(actual, 2),
                "expected_bill": round(total_expected, 2),
                "variance": round(actual - total_expected, 2),
                "trace": trace,
            }

        total_expected = 0.0
        trace: List[str] = [f"Schedule: {sc_code} ({billing_type})"]

        tiered_steps = [s for s in logic_steps if _is_tiered_step(s.get("step_name", ""))]
        is_summer = _is_summer_bill(bill_date)
        flat_steps = []
        for s in logic_steps:
            if _is_tiered_step(s.get("step_name", "")):
                continue

            variant = _is_metered_variant(s.get("step_name", ""))
            if variant is not None and variant != is_metered:
                continue  # this is the OTHER variant, doesn't apply to this account

            season = _seasonal_variant(s.get("step_name", ""))
            if season is not None:
                if is_summer is None:
                    # bill month unknown -- default to "base" rather than
                    # silently including both (matches the safer of the
                    # two options; base/winter rates are typically the
                    # lower of the pair, so this under- rather than
                    # over-estimates when the month can't be determined)
                    if season != "base":
                        continue
                    logger.warning(
                        f"No usable bill date for {sc_code} -- cannot determine "
                        f"summer/base season for '{s.get('step_name')}'. "
                        f"Defaulting to the base/winter rate."
                    )
                elif (season == "summer") != is_summer:
                    continue  # wrong season for this bill's month

            flat_steps.append(s)

        for step in flat_steps:
            name = step.get("step_name", "Step")
            ctype = (step.get("charge_type") or "").strip()
            rate = float(step.get("value", 0) or 0)

            if ctype == "fixed_fee":
                cost = rate
            elif ctype == "per_kwh":
                cost = rate * billed_kwh
            elif ctype == "per_kw":
                cost = rate * billed_demand
            else:
                continue

            total_expected += cost
            trace.append(f"{name}: ${cost:.2f}")

        if tiered_steps:
            tier_cost, tier_trace = _compute_tiered_es_charge(tiered_steps, billed_kwh, billed_demand)
            total_expected += tier_cost
            trace.extend(tier_trace)

        bill_dt_for_riders = bill_date if isinstance(bill_date, (pd.Timestamp, datetime)) else None
        if bill_dt_for_riders is not None and pd.isna(bill_dt_for_riders):
            bill_dt_for_riders = None  
        if bill_dt_for_riders is None:
            logger.warning(
                f"No usable bill date for {sc_code} -- cannot check rider withdrawal "
                f"dates, so withdrawn riders will still be INCLUDED. Pass a real "
                f"'bill_date'/'read_date' on the row to enable this check."
            )

        rider_total = 0.0
        riders_applied = 0
        riders_skipped_withdrawn = []
        for r in riders:
            withdrawn_date = _parse_effective_date(r.get("withdrawn_date"))
            if withdrawn_date and bill_dt_for_riders is not None:
                bill_date_only = (
                    bill_dt_for_riders.date()
                    if isinstance(bill_dt_for_riders, datetime) else bill_dt_for_riders
                )
                if withdrawn_date <= bill_date_only:
                    riders_skipped_withdrawn.append(r.get("rider_name"))
                    continue

            rate = float(r.get("value", 0) or 0)
            unit = (r.get("unit") or "").lower()
            if unit == "kwh":
                cost = rate * billed_kwh
            elif unit == "kw":
                cost = rate * billed_demand
            else:
                logger.warning(f"Unknown rider unit '{unit}' for {r.get('rider_name')}, skipping")
                continue
            rider_total += cost
            riders_applied += 1

        if rider_total:
            total_expected += rider_total
            trace.append(f"Total Riders ({riders_applied} applied): ${rider_total:.2f}")
        if riders_skipped_withdrawn:
            trace.append(f"Riders excluded as withdrawn by bill date: {', '.join(riders_skipped_withdrawn)}")

       
        min_candidates = [
            float(s.get("value", 0) or 0)
            for s in logic_steps
            if (s.get("charge_type") or "") in {"minimum_charge", "minimum_bill"}
        ]
        if min_candidates:
            min_required = max(min_candidates)
            if total_expected < min_required:
                total_expected = min_required
                trace.append(f"Minimum bill enforced: ${min_required:.2f}")

        actual = float(row.get("bill_amount", 0) or 0)
        variance = actual - total_expected

        return {
            "status": "SUCCESS",
            "sc_code": sc_code,
            "billed_schedule": billed_schedule,
            "billing_type": billing_type,
            "actual_bill": round(actual, 2),
            "expected_bill": round(total_expected, 2),
            "variance": round(variance, 2),
            "trace": trace,
        }
