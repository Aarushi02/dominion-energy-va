# ------------------------------------------------------------------
# Dominion/VEPGA Audit Engine
# Adapted from National Grid's calc_engine_updated.py, restructured
# for the shape our tariff_pipeline_v2.py actually produces.
#
# Key differences from the National Grid engine, and why:
#
# 1. sc_code format: "SCHEDULE 100" not "SC1" -- normalization changed
#    to match, and downstream billing data must use the same "Schedule
#    <code>" style (e.g. from the Dominion Account Profile "Current
#    Rate" field, which literally reads "Schedule 110").
#
# 2. Multiple logic entries per sc_code, selected by BILLING TYPE not
#    effective_date. Our SCHEDULE 100 has two entries ("Non-Demand
#    Billing" / "Demand Billing") because the tariff itself branches
#    on usage threshold (< 10,000 kWh vs >= 10,000 kWh), not because
#    they're different historical versions of the same rate. National
#    Grid's engine has no equivalent -- it assumes one active logic
#    version at a time, picked by date. We still support effective_date
#    versioning IF the field exists (future-proofing, since Stage 3
#    doesn't currently extract it), but billing-type selection happens
#    first and is the primary selector here.
#
# 3. Riders are a SEPARATE array (riders_priced), not folded into
#    logic_steps. National Grid's engine has no rider concept at all --
#    this is new logic, modeled on how the original app_new.py added
#    rider_total_per_kwh/per_kw on top of base charges per schedule.
#
# 4. Tiered ES charges are DEMAND-BUCKETED (150 kWh per kW per tier),
#    not simple flat per_kwh charges. A step named "...(First 150 kWh
#    per kW)" means: for every kW of billed demand, the first 150 kWh
#    of usage is billed at this tier's rate, the next 150 kWh per kW
#    at the next tier's rate, and so on, with the last tier open-ended
#    ("Additional kWh"). This mirrors the tiered ES bucket algorithm
#    in the original repo's app_new.py (schedule_100/schedule_110), NOT
#    National Grid's flat _select_rate_by_voltage() model, since
#    Dominion's tiering is demand-based, not voltage-based.
#
# 5. Minimum-charge enforcement is currently a no-op: Stage 3's prompt
#    doesn't extract minimum-charge values as a distinct charge_type
#    yet, so there's nothing to enforce. Kept as a hook, not removed,
#    so it activates automatically once/if that extraction is added.
# ------------------------------------------------------------------

import json
import logging
from datetime import datetime, date
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pandas as pd

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------

def _normalize_sc_code(sc: Optional[str]) -> str:
    """
    'Schedule 110', 'SCHEDULE 110', 'schedule-110' all normalize to
    the same key, matching however the billing data happens to spell
    the rate schedule (e.g. Dominion Account Profile PDFs print
    "Current Rate: Schedule 110").
    """
    if sc is None:
        return ""
    s = str(sc).upper().replace("-", " ")
    s = " ".join(s.split())  # collapse repeated whitespace
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
    eff = item.get("effective_date") or (item.get("metadata") or {}).get("effective_date")
    return _parse_effective_date(eff)


# ---------------------------------------------------------------------
# Billing-type (Non-Demand vs Demand) selection
# ---------------------------------------------------------------------

def _classify_billing_type(description: Optional[str]) -> str:
    """
    Maps a logic entry's free-text 'description' field to a normalized
    billing type. Falls back to 'unknown' rather than guessing, since
    Stage 3's description wording could vary slightly across schedules
    (e.g. 'Demand Billing' vs 'Demand-Metered Billing').
    """
    if not description:
        return "unknown"
    d = description.upper()
    if "NON-DEMAND" in d or "NON DEMAND" in d:
        return "non_demand"
    if "DEMAND" in d:
        return "demand"
    return "unknown"


def _select_billing_type_for_row(row: pd.Series) -> str:
    """
    Mirrors the tariff's own rule (confirmed directly from SCHEDULE
    100's text): non-demand if usage < 10,000 kWh, demand otherwise.
    A row can override this directly via an explicit 'is_demand'
    column if the caller already knows the account's billing type
    (e.g. from Dominion's own 'Current Rate' / meter-type fields)
    rather than inferring it from a single month's usage.
    """
    if "is_demand" in row and pd.notna(row["is_demand"]):
        return "demand" if bool(row["is_demand"]) else "non_demand"

    kwh = float(row.get("billed_kwh", 0) or 0)
    return "demand" if kwh >= 10000 else "non_demand"


def _is_metered_variant(step_name: str) -> Optional[bool]:
    """
    Some schedules extract TWO customer-charge steps -- one for
    Metered accounts, one for Unmetered -- as separate logic_steps
    entries (both charge_type='fixed_fee'). Applying both blindly
    double-counts the customer charge (confirmed bug: was adding
    $7.59 + $2.76 instead of picking one). Returns True/False if the
    step name marks a variant, None if it's not a metered/unmetered
    variant at all (i.e. a normal, unconditional step).
    """
    name = step_name.lower()
    if "unmetered" in name:
        return False
    if "metered" in name:  # note: "metered" also matches inside "unmetered", so this check must come second
        return True
    return None


# ---------------------------------------------------------------------
# Tiered ES charge bucketing (150 kWh per kW per tier)
# ---------------------------------------------------------------------

TIER_BUCKET_KWH_PER_KW = 150


def _is_tiered_step(step_name: str) -> bool:
    name = step_name.lower()
    return any(kw in name for kw in ("first 150", "next 150", "additional"))


def _compute_tiered_es_charge(tier_steps: List[dict], billed_kwh: float, billed_demand: float) -> tuple:
    """
    Applies the demand-bucketed tier algorithm: each tier (except the
    last, "Additional") covers up to (150 kWh x billed_demand_kW) of
    usage, billed at that tier's rate; remaining usage falls into the
    next tier, with the final "Additional" tier open-ended.

    Returns (total_cost, trace_lines).
    """
    if billed_demand <= 0:
        logger.warning("Tiered ES charge requested but billed_demand is 0 -- "
                        "all usage will fall into the open-ended 'Additional' tier "
                        "since no bucket capacity exists without demand.")

    remaining = billed_kwh
    total = 0.0
    trace = []

    # sort tiers: First, Next, Next, ..., Additional last (assume input
    # order matches document order, which Stage 3's prompt preserves)
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


# ---------------------------------------------------------------------
# Audit Engine
# ---------------------------------------------------------------------

class DominionAuditEngine:
    """
    Tariff audit engine for Dominion/VEPGA schedules, built from the
    output of dominion_tariff_pipeline_v2.py (final_tariff_logic.json).
    """

    def __init__(self, tariff_definitions_path: str):
        self.tariff_map = self._load_logic(tariff_definitions_path)

    # -----------------------------------------------------------------
    # Load tariff logic
    # -----------------------------------------------------------------

    def _load_logic(self, path: str) -> Dict[str, List[dict]]:
        with open(path, "r") as f:
            data = json.load(f)

        if isinstance(data, dict) and "tariffs" in data:
            data = data["tariffs"]

        mapping: Dict[str, List[dict]] = {}
        for item in data:
            sc = _normalize_sc_code(item.get("sc_code") or item.get("source_schedule"))
            item["_effective_date"] = _extract_effective_date(item)
            item["_billing_type"] = _classify_billing_type(item.get("description"))
            mapping.setdefault(sc, []).append(item)

        # sort each schedule's entries by effective_date where present,
        # so _pick_logic_for_bill's date-based fallback behaves sensibly
        # once/if effective_date extraction is added upstream
        for sc, items in mapping.items():
            items.sort(key=lambda x: (x["_effective_date"] is None, x["_effective_date"] or date.min))

        return mapping

    # -----------------------------------------------------------------
    # Pick the correct logic entry for a bill: billing type first,
    # then effective date within that type if multiple versions exist
    # -----------------------------------------------------------------

    def _pick_logic_for_bill(self, sc_code: str, billing_type: str, bill_dt) -> Optional[dict]:
        versions = self.tariff_map.get(sc_code)
        if not versions:
            return None

        # filter to matching billing type first; if the schedule has no
        # non-demand/demand split at all (single logic entry, billing
        # type 'unknown'), just use whatever's there
        matching = [v for v in versions if v["_billing_type"] == billing_type]
        if not matching:
            matching = versions  # schedule doesn't branch by billing type

        if not bill_dt:
            return matching[-1]

        if isinstance(bill_dt, datetime):
            bill_dt = bill_dt.date()

        dated = [v for v in matching if v["_effective_date"] and v["_effective_date"] <= bill_dt]
        return max(dated, key=lambda v: v["_effective_date"]) if dated else matching[0]

    # -----------------------------------------------------------------
    # Core calculation
    # -----------------------------------------------------------------

    def calculate_expected_bill(self, row: pd.Series) -> dict:
        sc_code = _normalize_sc_code(row.get("service_class") or row.get("rate_schedule") or row.get("current_rate"))
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
        is_metered = bool(row.get("is_metered", True))  # default: assume metered unless told otherwise

        total_expected = 0.0
        trace: List[str] = [f"Schedule: {sc_code} ({billing_type})"]

        # -------------------------------------------------------------
        # Base tariff steps -- separate tiered ES steps from flat ones,
        # and skip whichever metered/unmetered customer-charge variant
        # doesn't apply to this account (see _is_metered_variant)
        # -------------------------------------------------------------

        tiered_steps = [s for s in logic_steps if _is_tiered_step(s.get("step_name", ""))]
        flat_steps = []
        for s in logic_steps:
            if _is_tiered_step(s.get("step_name", "")):
                continue
            variant = _is_metered_variant(s.get("step_name", ""))
            if variant is not None and variant != is_metered:
                continue  # this is the OTHER variant, doesn't apply to this account
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

        # -------------------------------------------------------------
        # Riders -- summed on top of base charges, per unit type
        # -------------------------------------------------------------

        rider_total = 0.0
        for r in riders:
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

        if rider_total:
            total_expected += rider_total
            trace.append(f"Total Riders ({len(riders)} applied): ${rider_total:.2f}")

        # -------------------------------------------------------------
        # Minimum bill enforcement -- currently a no-op (see module
        # docstring: Stage 3 doesn't extract minimum-charge values yet)
        # -------------------------------------------------------------

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
            "billing_type": billing_type,
            "actual_bill": round(actual, 2),
            "expected_bill": round(total_expected, 2),
            "variance": round(variance, 2),
            "trace": trace,
        }
