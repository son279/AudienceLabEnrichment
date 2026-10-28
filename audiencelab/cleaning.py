"""
audiencelab.cleaning
--------------------
Business-agnostic cleaning for purchased audience lists.

Nothing in here knows about a service area, a scoring model, or a specific
client. It loads the audience file, standardizes text, phones, emails,
addresses, income/age fields, flags each row as residential and/or commercial,
and removes duplicate people.

Typical use:
    raw = load_audience("audience.csv", extra_required=[...client columns...])
    df = clean_general(raw, report)

Shared by every client (clients/baltz, clients/mvt, ...).
"""
import re
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

# ============================================================
# CONFIG
# ============================================================

# Default input file; callers can pass any path to load_audience().
DEFAULT_AUDIENCE_PATH = "audience.csv"

# Columns clean_general() reads directly. Client scripts add their own via
# load_audience(..., extra_required=[...]).
GENERAL_REQUIRED_COLS = [
    "UUID", "FIRST_NAME", "LAST_NAME",
    "PERSONAL_ADDRESS", "PERSONAL_CITY", "PERSONAL_STATE", "PERSONAL_ZIP",
    "SKIPTRACE_ADDRESS", "SKIPTRACE_CITY", "SKIPTRACE_STATE", "SKIPTRACE_ZIP",
    "AGE_RANGE", "INCOME_RANGE", "NET_WORTH",
]

# Common spellings in purchased lists -> canonical city name
CITY_ALIASES = {
    "w memphis": "west memphis", "w. memphis": "west memphis",
    "olivebranch": "olive branch", "hornlake": "horn lake",
    "germantwn": "germantown",
}

STATE_NAMES = {
    "TENNESSEE": "TN", "MISSISSIPPI": "MS", "ARKANSAS": "AR", "ALABAMA": "AL",
    "KENTUCKY": "KY", "MISSOURI": "MO", "GEORGIA": "GA", "TEXAS": "TX", "LOUISIANA": "LA",
    "FLORIDA": "FL", "CALIFORNIA": "CA", "NEW YORK": "NY", "ILLINOIS": "IL", "OHIO": "OH",
    "NORTH CAROLINA": "NC", "SOUTH CAROLINA": "SC", "VIRGINIA": "VA", "INDIANA": "IN",
    "MICHIGAN": "MI", "PENNSYLVANIA": "PA", "NEW JERSEY": "NJ", "WASHINGTON": "WA",
    "COLORADO": "CO", "ARIZONA": "AZ", "OKLAHOMA": "OK", "MASSACHUSETTS": "MA",
    "MINNESOTA": "MN", "WISCONSIN": "WI", "MARYLAND": "MD", "DISTRICT OF COLUMBIA": "DC",
}

# Street addresses that are offices, not homes.
BUSINESS_ADDRESS_RE = r"\b(?:ste|suite|bldg|building|floor|fl)\b\.?\s*\w*"

# A number with no DNC flag is NOT called. (Treating an unknown flag as
# callable is a TCPA risk.)
UNKNOWN_DNC_IS_BLOCKED = True

EMAIL_RE = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")
JUNK_EMAIL_DOMAINS = {"example.com", "test.com", "email.com", "none.com", "noemail.com"}
BLANK_VALUES = {"", "n/a", "na", "none", "null", "nan", "unknown", "-", "--"}
TRUE_VALUES = {"y", "yes", "true", "t", "1"}

DROP_COLS = [
    "SHA256_PERSONAL_EMAIL", "SHA256_BUSINESS_EMAIL", "SKIPTRACE_RELIGION_CODE",
    "SKIPTRACE_ETHNIC_CODE", "SKIPTRACE_LANGUAGE_CODE", "SKIPTRACE_IP", "SKILLS",
    "INTERESTS", "TWITTER_URL", "FACEBOOK_URL", "SOCIAL_CONNECTIONS",
    "INFERRED_YEARS_EXPERIENCE", "EDUCATION_HISTORY", "SKIPTRACE_LANDLINE_NUMBERS",
    "SKIPTRACE_WIRELESS_NUMBERS", "COMPANY_NAME_HISTORY", "JOB_TITLE_HISTORY",
]

PHONE_COLS = [("MOBILE_PHONE", "MOBILE_PHONE_DNC"),
              ("DIRECT_NUMBER", "DIRECT_NUMBER_DNC"),
              ("PERSONAL_PHONE", "PERSONAL_PHONE_DNC")]
EMAIL_COLS = ["PERSONAL_VERIFIED_EMAILS", "BUSINESS_VERIFIED_EMAILS",
              "BUSINESS_EMAIL", "PERSONAL_EMAILS"]


# ============================================================
# FIELD HELPERS
# ============================================================


def blank_to_nan(df):
    """Strip every text cell and turn placeholder values ('', 'N/A', 'null'...) into NaN,
    so fillna() fallbacks and notna() checks actually work."""
    for col in df.columns:
        if df[col].dtype == object or pd.api.types.is_string_dtype(df[col]):
            s = df[col].astype("string").str.strip().str.replace(r"\s+", " ", regex=True)
            df[col] = s.mask(s.str.lower().isin(BLANK_VALUES)).astype(object)
    return df


def split_list(value):
    if value is None or (isinstance(value, float) and np.isnan(value)) or value is pd.NA:
        return []
    return [v.strip() for v in str(value).split(",") if v.strip()]


def smart_title(s):
    """Title-case only text that is ALL CAPS or all lower, so 'McDonald', 'DeSoto'
    and 'Apt 4B' that are already mixed-case stay as they are."""
    s = s.astype("string")
    shout = s.str.isupper() | s.str.islower()
    titled = (s.str.title()
              .str.replace(r"\bMc([a-z])", lambda m: "Mc" + m.group(1).upper(), regex=True)
              .str.replace(r"(\d)([A-Z])\b", lambda m: m.group(1) + m.group(2), regex=True))
    return s.where(~shout.fillna(False), titled).astype(object)


def normalize_phone(raw):
    """Any US format -> +1XXXXXXXXXX, or None if it is not a valid NANP number."""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    # Area code and exchange cannot start with 0/1; 555-0100..0199 is fictional.
    if digits[0] in "01" or digits[3] in "01" or (digits[3:6] == "555" and digits[6:8] == "01"):
        return None
    return "+1" + digits


def callable_phones(row):
    """Every non-DNC number in priority order: mobile, then direct, then personal."""
    out = []
    fill = "Y" if UNKNOWN_DNC_IS_BLOCKED else "N"
    for num_col, dnc_col in PHONE_COLS:
        nums = split_list(row.get(num_col))
        flags = [f.upper() for f in split_list(row.get(dnc_col))]
        flags += [fill] * (len(nums) - len(flags))
        for num, flag in zip(nums, flags):
            phone = normalize_phone(num)
            if phone and flag in {"N", "NO", "FALSE", "0"} and phone not in out:
                out.append(phone)
    return out


def first_clean_phone(row):
    """Best callable number, preferring ones the vendor lists in VALID_PHONES.
    Returns (phone, validated)."""
    phones = callable_phones(row)
    valid = {normalize_phone(p) for p in split_list(row.get("VALID_PHONES"))}
    for phone in phones:
        if phone in valid:
            return phone, True
    return (phones[0], False) if phones else (np.nan, False)


def first_valid_phone(value):
    """Company phones sometimes carry an extension glued on (+1877...11502)."""
    for num in split_list(value):
        phone = normalize_phone(num)
        if phone:
            return phone
    return np.nan


def normalize_state(s):
    """'TENNESSEE' / 'tn' -> 'TN'. Unknown full names become blank rather than a
    wrong 2-letter guess."""
    s = s.astype("string").str.upper().str.strip()
    mapped = s.map(STATE_NAMES)
    return mapped.where(mapped.notna(), s.where(s.str.len() == 2)).astype(object)


def first_clean_email(row):
    for col in EMAIL_COLS:
        for email in split_list(row.get(col)):
            email = email.strip().strip("<>").lower()
            if EMAIL_RE.match(email) and email.split("@")[1] not in JUNK_EMAIL_DOMAINS:
                return email
    return np.nan


def range_midpoint(text):
    """'$45,000 to $59,999' -> 52,500; 'less than $20,000' -> 10,000;
    '$250,000+' / '$1,000,000 or more' -> lower bound; '-$2,500 to $2,499' -> 0."""
    if text is None or (isinstance(text, float) and np.isnan(text)):
        return np.nan
    text = re.sub(r"(\d)\s*k\b", r"\1,000", str(text).lower())
    nums = []
    for m in re.finditer(r"(-?)\$?\s*(\d[\d,]*)", text):
        # A dash is a minus sign only at the start or after "to" -- in
        # "$2,500-$4,999" it is a separator.
        before = text[:m.start()].rstrip()
        negative = m.group(1) == "-" and (before == "" or before.endswith("to"))
        value = float(m.group(2).replace(",", "") or 0)
        nums.append(-value if negative else value)
    if not nums:
        return np.nan
    if len(nums) == 1:
        return nums[0] / 2 if ("less" in text or "under" in text) else nums[0]
    return (nums[0] + nums[1]) / 2


def normalize_city(s, aliases=CITY_ALIASES):
    s = (s.astype("string").str.lower()
         .str.replace(r"[^a-z\s.]", "", regex=True).str.replace(r"\s+", " ", regex=True)
         .str.strip())
    return s.replace(aliases)


def zip5(series):
    """Keeps leading zeros and handles 38103.0 / 38103-1234 / ' 38103 '."""
    s = series.astype("string").str.replace(r"\.0$", "", regex=True)
    return s.str.extract(r"(\d{5})")[0].astype(object)


# ============================================================
# LOAD
# ============================================================


def load_audience(path=DEFAULT_AUDIENCE_PATH, extra_required=None):
    """Read an audience file (.csv, .tsv, .xlsx, .xls) with every column as text,
    so ZIP leading zeros and phone numbers survive untouched.

    Checks GENERAL_REQUIRED_COLS plus any client-specific `extra_required`
    columns up front, so a missing column gives a clear error instead of a
    KeyError halfway through cleaning."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Audience file not found: {path.resolve()}")

    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        df = pd.read_excel(path, dtype=str)
    elif suffix in {".csv", ".tsv", ".txt"}:
        df = pd.read_csv(path, dtype=str, sep="\t" if suffix == ".tsv" else ",",
                         encoding="utf-8-sig", on_bad_lines="warn")
    else:
        raise ValueError(f"Unsupported file type '{suffix}'. Use .csv, .tsv, .xlsx or .xls")

    df.columns = df.columns.str.strip().str.upper()  # tolerate header case/spacing
    required = list(GENERAL_REQUIRED_COLS) + list(extra_required or [])
    missing = [c for c in dict.fromkeys(required) if c not in df.columns]
    if missing:
        raise KeyError(f"Audience file is missing required columns: {', '.join(missing)}")
    print(f"Loaded {len(df):,} rows, {len(df.columns)} columns from {path.name}")
    return df


# ============================================================
# CLEAN
# ============================================================


def pick_address(df):
    """Use ONE complete source per row (personal, else skiptrace) so street, city,
    state and ZIP never come from two different addresses."""
    def has(prefix, street):
        located = df[f"{prefix}_CITY"].notna() | df[f"{prefix}_ZIP"].notna()
        return located & df[f"{prefix}_ADDRESS"].notna() if street else located

    # Priority: full personal address, full skiptrace address, then whichever
    # at least has a city/ZIP (personal first).
    choice = np.select(
        [has("PERSONAL", True), has("SKIPTRACE", True),
         has("PERSONAL", False), has("SKIPTRACE", False)],
        ["personal", "skiptrace", "personal", "skiptrace"],
        default="",
    )
    use_personal = choice == "personal"
    out = pd.DataFrame(index=df.index)
    for field in ("ADDRESS", "CITY", "STATE", "ZIP"):
        out[field] = df[f"PERSONAL_{field}"].where(use_personal, df[f"SKIPTRACE_{field}"])
    out["ADDRESS_SOURCE"] = choice
    return out


def dedupe(df, report):
    """Keep the most complete row per person instead of an arbitrary one."""
    df = df.assign(_filled=df.notna().sum(axis=1)).sort_values("_filled", ascending=False)

    before = len(df)
    has_uuid = df["UUID"].notna()
    df = pd.concat([df[has_uuid].drop_duplicates("UUID"), df[~has_uuid]])
    report["duplicate UUID"] = before - len(df)

    # Same person = same name + (same address OR same phone OR same email).
    # Rows missing the second identifier are never merged on name alone.
    for label, col in (("address", "ADDRESS"), ("phone", "PHONE"), ("email", "EMAIL")):
        key = (df["FIRST_NAME"].astype("string").str.lower() + "|"
               + df["LAST_NAME"].astype("string").str.lower() + "|"
               + df[col].astype("string").str.lower())
        dup = key.notna() & key.duplicated()
        report[f"duplicate name+{label}"] = int(dup.sum())
        df = df[~dup]
    return df.drop(columns="_filled")


def clean_general(df: pd.DataFrame, report: Counter | None = None) -> pd.DataFrame:
    """Standardize every field and remove duplicate people. Does NOT filter by
    location or contactability -- that is in audiencelab.filtering.

    Adds: ADDRESS, CITY, STATE, ZIP, ADDRESS_SOURCE, PHONE, PHONE_VALIDATED, EMAIL,
    INCOME, NET_WORTH_NUM, AGE_LOW, IS_RESIDENTIAL, IS_COMMERCIAL.
    """
    report = report if report is not None else Counter()
    report["raw rows"] = len(df)

    df = df.drop(columns=DROP_COLS, errors="ignore").copy()
    df = blank_to_nan(df)

    for c in ("FIRST_NAME", "LAST_NAME"):
        df[c] = smart_title(df[c])

    addr = pick_address(df)
    df["ADDRESS"] = smart_title(addr["ADDRESS"])
    df["CITY"] = smart_title(addr["CITY"])
    df["STATE"] = normalize_state(addr["STATE"])
    df["ZIP"] = zip5(addr["ZIP"])
    df["ADDRESS_SOURCE"] = addr["ADDRESS_SOURCE"]

    if "COMPANY_CITY" in df:
        df["COMPANY_CITY"] = smart_title(df["COMPANY_CITY"])
    if "COMPANY_STATE" in df:
        df["COMPANY_STATE"] = normalize_state(df["COMPANY_STATE"])
    if "COMPANY_PHONE" in df:
        df["COMPANY_PHONE"] = df["COMPANY_PHONE"].map(first_valid_phone)
    if "HOMEOWNER" in df:
        df["HOMEOWNER"] = np.where(
            df["HOMEOWNER"].astype("string").str.lower().isin(TRUE_VALUES).fillna(False), "Y", "N")

    phone = df.apply(first_clean_phone, axis=1, result_type="expand")
    df["PHONE"], df["PHONE_VALIDATED"] = phone[0], phone[1].astype(bool)
    df["EMAIL"] = df.apply(first_clean_email, axis=1)
    df["INCOME"] = df["INCOME_RANGE"].map(range_midpoint)
    df["NET_WORTH_NUM"] = df["NET_WORTH"].map(range_midpoint)
    df["AGE_LOW"] = pd.to_numeric(
        df["AGE_RANGE"].astype("string").str.extract(r"(\d+)")[0], errors="coerce"
    ).astype(float)
    if "SKIPTRACE_EXACT_AGE" in df:  # fill a missing range from the exact age
        df["AGE_LOW"] = df["AGE_LOW"].fillna(
            pd.to_numeric(df["SKIPTRACE_EXACT_AGE"], errors="coerce").astype(float))

    # Lead type flags. A person can be both (e.g. a business owner with a home
    # address and a company), so these are independent, not mutually exclusive.
    addr_text = df["ADDRESS"].astype("string")
    business_addr = addr_text.str.contains(BUSINESS_ADDRESS_RE, case=False, regex=True).fillna(False)
    has_company = df["COMPANY_NAME"].notna() if "COMPANY_NAME" in df else pd.Series(False, index=df.index)
    df["IS_RESIDENTIAL"] = ~business_addr              # address is not an office/suite
    df["IS_COMMERCIAL"] = has_company | business_addr  # tied to a company or a business address

    return dedupe(df, report)


def print_report(report):
    print("Cleaning report")
    for k, v in report.items():
        print(f"  {k:34} {v:,}")
