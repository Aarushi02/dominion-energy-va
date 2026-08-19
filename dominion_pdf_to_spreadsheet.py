# Dominion Energy "Electric Account Profile" PDF -> Spreadsheet
# Handles OCR (the PDF has no text layer)

import os
import re
import sys
import unicodedata
from pathlib import Path

import pandas as pd
import pytesseract
from pytesseract import Output
from pdf2image import convert_from_path

# HELPERS 

def normspace(s: str) -> str:
    if s is None:
        return ""
    s = unicodedata.normalize("NFKC", str(s)).replace("\u00A0", " ")
    return re.sub(r"\s+", " ", s).strip()


def pull(text, pats, group=1, default=""):
    for pat in pats:
        m = re.search(pat, text, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return normspace(m.group(group))
    return default


MONEY_RE = r"\$?-?[\d,]*\.?\d+"


def parse_money(tok):
    if tok is None:
        return None
    s = str(tok).replace("$", "").replace(",", "").strip()
    if s in ("", "-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_intlike(tok):
    if tok is None:
        return None
    s = str(tok).replace(",", "").strip()
    if s in ("", "-"):
        return None
    try:
        return int(float(s))
    except ValueError:
        return None

# OCR

def ocr_pages(pdf_path, dpi=300):
    """Returns a list of page texts (index 0 = page 1)."""
    images = convert_from_path(pdf_path, dpi=dpi)
    pages = []
    for img in images:
        txt = pytesseract.image_to_string(img, config="--psm 6")
        pages.append(txt)
    return pages


def ocr_two_column_page(img, split_frac=0.42):

    w, h = img.size
    left = img.crop((0, 0, int(w * split_frac), h))
    right = img.crop((int(w * (split_frac - 0.03)), 0, w, h))
    left_txt = pytesseract.image_to_string(left, config="--psm 6")
    right_txt = pytesseract.image_to_string(right, config="--psm 6")
    return left_txt, right_txt

# PAGE 2: ACCOUNT PROFILE (key-value fields) + ANNUAL SUMMARY TABLE

def extract_account_profile(left_col_text, right_col_text, page1_text):
    """
    left_col_text  = OCR of the left column of page 2 (key-value fields)
    right_col_text = OCR of the right column of page 2 (manager box)
    page1_text     = OCR of the cover page (has the customer name cleanly)
    """
    fields = {
        "customer": pull(left_col_text, [r"Account Profile\s*\n\s*(.+?)\n\s*\nPhone Number"], default=""),
        "phone_number": pull(left_col_text, [r"Phone Number\s*\n?\s*(\(\d{3}\)\s?\d{3}-\d{4})"]),
        "mailing_address": pull(left_col_text, [r"Mailing Address\s*\n?\s*(.+?)\nCustomer Class"], default=""),
        "customer_class": pull(left_col_text, [r"Customer Class\s*\n?\s*(.+?)\n"]),
        "account_no": pull(left_col_text, [r"ACCOUNT NO\.?\s*\n?\s*(\d{6,15})"]),
        "service_address": pull(left_col_text, [r"Service Address\s*\n?\s*(.+?)\nTurn On Date"], default=""),
        "turn_on_date": pull(left_col_text, [r"Turn On Date\s*\n?\s*([A-Za-z]+ \d{1,2}, \d{4})"]),
        "district_office": pull(left_col_text, [r"District Office\s*\n?\s*(.+?)\n"]),
        "naics_code": pull(left_col_text, [r"NAICS Code\s*\n?\s*(\d+)"]),
        "meter_numbers": pull(left_col_text, [r"Meter Number\(s\)\s*\n?\s*(.+?)\nCurrent Rate"], default=""),
        "current_rate": pull(left_col_text, [r"Current Rate\s*\n?\s*(.+?)\n"]),
        "voltage": pull(left_col_text, [r"Voltage\s*\n?\s*(.+?)\n"]),
        "delivery_phase": pull(left_col_text, [r"Delivery Phase\s*\n?\s*(.+?)\n"]),
        "minimum_demand": pull(left_col_text, [r"Minimum Demand\s*\n?\s*(.+?)\n"]),
        "facility_charge": pull(left_col_text, [r"Facility Charge\s*\n?\s*(.+?)\n"]),
        "billing_status": pull(left_col_text, [r"Billing Status\s*\n?\s*(.+?)\n"]),
        "key_account_manager": pull(
            right_col_text,
            [r"\n([A-Z][A-Z]+(?: [A-Z][A-Z]+)+)\b[^\n]*\n(?:[^\n]*\n){0,2}[^\n]*Key Account Manager"],
        ),
    }
    # Multi-line address fields collapse newlines -> single spaced string
    fields["customer"] = normspace(fields["customer"])
    fields["mailing_address"] = normspace(fields["mailing_address"].replace("\n", " "))
    fields["service_address"] = normspace(fields["service_address"].replace("\n", " "))
    fields["meter_numbers"] = normspace(fields["meter_numbers"].replace("\n", "; "))
    return fields


def extract_annual_summary(text):
    """
    Parses rows like:
        2026 (YTD) $2,899.47 22,862 kWh 0.13 $/kWh
        2025 $4,620.41 34,895 kWh 0.13 $/kWh
    """
    rows = []
    pattern = re.compile(
        r"(?P<year>\d{4}(?:\s*\(YTD\))?)\s+"
        r"\$(?P<billed>[\d,]+\.\d{2})\s+"
        r"(?P<usage>[\d,]+)\s*kWh\s+"
        r"(?P<avg>[\d.]+)\s*\$/kWh",
        flags=re.IGNORECASE,
    )
    for m in pattern.finditer(text):
        rows.append({
            "year": m.group("year").strip(),
            "total_billed": parse_money(m.group("billed")),
            "total_usage_kwh": parse_intlike(m.group("usage")),
            "average_cost_per_kwh": float(m.group("avg")),
        })
    return pd.DataFrame(rows)

# MONTHLY DETAIL TABLES

MONTH_HEADERS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
                  "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
KNOWN_LABELS = [
    "Bill From", "Bill To", "Billing Days", "Billed Rate",
    "Total Consumption", "Demand",
    "Basic Cust. Charges", "Basic Customer Chg",
    "Energy Charges", "Energy DIS", "Energy ESS",
    "Rider B kWh", "Rider BW kWh", "Rider CCR", "Rider CE kWh",
    "Rider DIST kWh", "Rider E kWh", "Rider GEN kWh", "Rider GV kWh",
    "Rider OSW kWh", "Rider PIPP", "Rider PPA", "Rider R kWh",
    "Rider RBB kWh", "Rider RGGI", "Rider RPS", "Rider S kWh",
    "Rider SMR kWh", "Rider SNA kWh", "Rider U1 kWh", "Rider U2 kWh",
    "Rider US-2 kWh", "Rider US-3 kWh", "Rider US-4 kWh", "Rider W kWh",
    "Ridr GT", "Transmission Energy", "Fuel Charges", "Fuel Chg",
    "Other Charges/Credits", "Virginia Tax Surcharge",
    "Subtotal", "Facility Charges", "Total Charges",
]

_LABELS_SORTED = sorted(KNOWN_LABELS, key=len, reverse=True)


def extract_year_from_header(text):
    m = re.search(r"Historical Electricity Usage\s*-\s*(\d{4})", text)
    return m.group(1) if m else None


def _find_month_columns(word_data):
    cols = {}
    n = len(word_data["text"])
    for i in range(n):
        t = word_data["text"][i].strip()
        if t in MONTH_HEADERS:  # exact-case match against all-caps list
            x_center = word_data["left"][i] + word_data["width"][i] / 2
            cols[t] = x_center
    return cols


def _match_label(tokens):
    tokens = list(tokens)
    while tokens and re.fullmatch(r"\*+", tokens[0]["text"]):
        tokens = tokens[1:]

    text_joined = " ".join(t["text"] for t in tokens)
    for label in _LABELS_SORTED:
        if text_joined.upper().startswith(label.upper()):
            consumed = len(label.split())
            return label, tokens[consumed:]
    return None, tokens


_NOISE_ROW_PATTERNS = [
    r"^LOUISA,?\s*COUNTY OF",           # repeated page header
    r"^Historical Electricity Usage",    # section title (has a year in it)
    r"Dominion Energy",                  # footer stamp
    r"Page\s+\d+\s+of\s+\d+",            # page X of Y footer
]


def _is_noise_row(raw_text):
    return any(re.search(pat, raw_text, flags=re.IGNORECASE) for pat in _NOISE_ROW_PATTERNS)


def _looks_like_data_row(tokens):
    for t in tokens:
        txt = t["text"]
        if re.fullmatch(MONEY_RE, txt) or re.fullmatch(r"\d{1,2}/\d{1,2}/\d{2,4}", txt) or re.fullmatch(r"VE-\d+", txt):
            return True
    return False


def extract_monthly_tables(pdf_images, page_texts, warn_unmatched=True):
    all_rows = []
    unmatched_summary = []

    for img, page_text in zip(pdf_images, page_texts):
        if "Historical Electricity Usage" not in page_text:
            continue

        year = extract_year_from_header(page_text)
        data = pytesseract.image_to_data(img, config="--psm 6", output_type=Output.DICT)
        n = len(data["text"])

        month_cols = _find_month_columns(data)
        if not month_cols:
            continue
        spacings = sorted(month_cols.values())
        gaps = [b - a for a, b in zip(spacings, spacings[1:])]
        max_dist = (sum(gaps) / len(gaps)) / 2 if gaps else 100

        rows = {}
        for i in range(n):
            txt = data["text"][i].strip()
            if not txt:
                continue
            key = (data["block_num"][i], data["par_num"][i], data["line_num"][i])
            rows.setdefault(key, []).append({
                "text": txt,
                "left": data["left"][i],
                "width": data["width"][i],
            })

        for key, tokens in rows.items():
            tokens = sorted(tokens, key=lambda t: t["left"])
            label, value_tokens = _match_label(tokens)
            if label is None:
                raw_text = " ".join(t["text"] for t in tokens)
                if _looks_like_data_row(tokens) and not _is_noise_row(raw_text):
                    unmatched_summary.append({"year": year, "row_text": raw_text})
                continue

            for vt in value_tokens:
                x_center = vt["left"] + vt["width"] / 2
                nearest_month, nearest_dist = None, None
                for month, cx in month_cols.items():
                    d = abs(x_center - cx)
                    if nearest_dist is None or d < nearest_dist:
                        nearest_month, nearest_dist = month, d
                if nearest_month is None or nearest_dist > max_dist:
                    continue  
                tok = vt["text"]
                if label in ("Bill From", "Bill To", "Billed Rate"):
                    value = tok
                elif label in ("Billing Days",):
                    value = parse_intlike(tok)
                elif label in ("Total Consumption", "Demand"):
                    value = parse_intlike(tok)
                else:
                    value = parse_money(tok)

                all_rows.append({
                    "year": year,
                    "month": nearest_month,
                    "line_item": label,
                    "value": value,
                })

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.drop_duplicates(subset=["year", "month", "line_item"], keep="first")

    if warn_unmatched and unmatched_summary:
        print(f"  {len(unmatched_summary)} row(s) with data-like tokens did not match "
              f"any label in KNOWN_LABELS -- these rows were SKIPPED, not extracted:")
        for u in unmatched_summary:
            print(f"    [{u['year']}] {u['row_text']}")
        print("    -> if any of these are real charge/rider line items, add them "
              "to KNOWN_LABELS and rerun.")

    return df


def pivot_monthly_wide_by_year(monthly_long_df):
    if monthly_long_df.empty:
        return {}

    label_order = {label: i for i, label in enumerate(KNOWN_LABELS)}
    out = {}

    for year, group in monthly_long_df.groupby("year"):
        wide = group.pivot_table(
            index="line_item",
            columns="month",
            values="value",
            aggfunc="first",
        )
        months_present = [m for m in MONTH_HEADERS if m in wide.columns]
        wide = wide.reindex(columns=months_present)

        wide = wide.reindex(
            sorted(wide.index, key=lambda li: label_order.get(li, len(label_order)))
        )
        wide = wide.reset_index()
        out[year] = wide

    return out

    main()
