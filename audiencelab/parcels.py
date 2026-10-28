"""
audiencelab.parcels
-------------------
Matching cleaned residential leads to Shelby County assessor parcels (the CSV
written by audiencelab.shelby_lookup). Shared by every client; what a client
DOES with the parcel facts (exclusions, scoring, tiers) lives in
clients/<name>/enrich.py.

  load_step1()            read a client's cleaned residential_leads.csv
  load_property_lookup()  one parcel per lead, with a PARCEL_CONFIDENT flag
  enrich_with_parcels()   left-join parcel facts + OWNER_MATCH / OWNER_MATCHES_LEAD
  split_by_exclusion()    kept / company-owned / other-excluded
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

from audiencelab.cleaning import range_midpoint, zip5
from audiencelab.filtering import UNIT_ADDRESS_RE, street_key

# ============================================================
# CONFIG
# ============================================================

STEP1_REQUIRED_COLS = [
    "FIRST_NAME", "LAST_NAME", "ADDRESS", "ZIP", "PHONE", "EMAIL",
    "INCOME_RANGE", "NET_WORTH",
]
LOOKUP_REQUIRED_COLS = [
    "LEAD_NAME", "LEAD_ADDRESS", "Parcel ID", "Property Address", "Owner Name",
    "Owner Mailing Address", "Land Square Footage", "Year Built", "Extra Features",
    "Last Sale Type", "Last Sale Price", "Years Owned",
]

# Assessor sale types that are real purchases when the price is > 0.
# QC (quitclaim) with a $0 price is a family/trust transfer, not a purchase.
ARMS_LENGTH_TYPES = {"WD", "SW"}

# Directions are dropped and suffixes shortened before comparing street names, so
# "N Strathmore Cir" equals the assessor's "STRATHMORE CL N".
_DIRECTIONS = {"n", "s", "e", "w", "ne", "nw", "se", "sw"}
_SUFFIXES = {
    "street": "st", "drive": "dr", "lane": "ln", "road": "rd", "cove": "cv",
    "avenue": "ave", "circle": "cir", "cl": "cir",  # assessor writes CL for circle
    "court": "ct", "boulevard": "blvd", "place": "pl", "terrace": "ter",
    "parkway": "pkwy", "highway": "hwy", "trail": "trl",
}
_NAME_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

LEGAL_ENTITY_RE = r"\b(?:LLC|INC|LP|LLP|LTD|CORP)\b"
ENTITY_WORD_RE = (
    r"\b(?:HOLDINGS|PROPCO|CAPITAL|INVESTMENTS?|PROPERT(?:Y|IES)|MANAGEMENT|SFR|HOMES|"
    r"ENTERPRISES|HOUSING AUTHORITY|STATE OF|CITY OF|COUNTY|SCHOOL|CHURCH|MINISTRIES|"
    r"BANK|MORTGAGE|SECRETARY)\b"
)
TRUST_RE = r"\bTRUST\b"

GARAGE_RE = "GARAGE|CARPORT"
OUTBUILDING_RE = "GARAGE|CARPORT|SHED|BARN|WORKSHOP|STORAGE|POLE"


# ============================================================
# SMALL HELPERS
# ============================================================


def _missing(value):
    return value is None or value is pd.NA or (isinstance(value, float) and np.isnan(value))


def to_num(value):
    """'$1,500' / '12.5' / NaN -> float or NaN."""
    if _missing(value):
        return np.nan
    try:
        return float(re.sub(r"[^\d.\-]", "", str(value)))
    except ValueError:
        return np.nan


def street_tokens(address):
    t = re.sub(r"[^a-z0-9 ]", " ", str(address).lower()).split()
    return [_SUFFIXES.get(x, x) for x in t if x not in _DIRECTIONS]


def same_street(a, b):
    """Street number, street name and suffix all agree (directions/abbreviations ignored)."""
    ta, tb = street_tokens(a), street_tokens(b)
    return bool(ta) and bool(tb) and ta[:2] == tb[:2] and ta[-1] == tb[-1]


def same_street_number(a, b):
    ta, tb = street_tokens(a), street_tokens(b)
    return bool(ta) and bool(tb) and ta[0] == tb[0]


def owner_has_surname(owner, first, last):
    """True if the lead's surname appears as a whole word in the assessor's owner name."""
    if _missing(owner):
        return False
    words = set(re.sub(r"[^A-Z ]", " ", str(owner).upper()).split())
    tokens = [t for t in re.sub(r"[^a-z ]", " ", str(last).lower()).split()
              if t not in _NAME_SUFFIXES]
    if not tokens:  # "Lawrence Ii" style rows where the surname landed in FIRST_NAME
        tokens = re.sub(r"[^a-z ]", " ", str(first).lower()).split()[:1]
    return any(t.upper() in words for t in tokens)


def owner_surname(owner):
    """The assessor writes owners as 'LAST FIRST MIDDLE [& SPOUSE]', so the
    first word is the primary owner's surname. '' if there is no name."""
    if _missing(owner):
        return ""
    words = re.sub(r"[^A-Z ]", " ", str(owner).upper()).split()
    return words[0] if words else ""


def owner_has_reversed_name(owner, first, last):
    """True if the lead's first and last names look swapped: the lead's FIRST_NAME
    is the owner's surname. Catches leads stored as 'Davis Garry' whose deed
    owner is the spouse ('DAVIS REBECCA Y'), which the surname check misses."""
    surname = owner_surname(owner)
    tokens = [t for t in re.sub(r"[^a-z ]", " ", str(first).lower()).split()
              if t not in _NAME_SUFFIXES]
    return bool(surname) and bool(tokens) and tokens[0].upper() == surname


def owner_match(owner, first, last, mailing, prop_address):
    """How the deed owner ties to the lead: 'surname', 'reversed name',
    'mailing address' (owner's mail goes to the property itself) or '' (no tie)."""
    if owner_has_surname(owner, first, last):
        return "surname"
    if owner_has_reversed_name(owner, first, last):
        return "reversed name"
    if same_street_number(mailing, prop_address):
        return "mailing address"
    return ""


def owner_type(name):
    """'person', 'trust' (living/family trusts are homeowners), 'entity' (LLCs,
    companies, housing authority, banks, churches...) or 'unknown'."""
    if _missing(name):
        return "unknown"
    o = str(name).upper()
    if re.search(LEGAL_ENTITY_RE, o):
        return "entity"
    if re.search(TRUST_RE, o):
        return "trust"
    if re.search(ENTITY_WORD_RE, o):
        return "entity"
    return "person"


def name_key(first, last):
    s = first.astype("string").fillna("") + " " + last.astype("string").fillna("")
    s = s.str.lower().str.replace(r"[^a-z ]", "", regex=True).str.replace(r"\s+", " ", regex=True)
    return s.str.strip()


def lead_key(first, last, address, unit_re=UNIT_ADDRESS_RE):
    return (name_key(first, last) + "|" + street_key(address, unit_re)).astype(object)


def keep_existing(df, cols):
    return df[[c for c in cols if c in df.columns]]


# ============================================================
# LOAD
# ============================================================


def load_step1(path):
    """Read a client's cleaned residential_leads.csv and rebuild the numeric
    fields scoring needs (income / net worth midpoints, ZIP5)."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"'{path}' not found. Run the client's clean step first.")
    df = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    missing = [c for c in STEP1_REQUIRED_COLS if c not in df.columns]
    if missing:
        raise KeyError(f"{path.name} is missing columns: {', '.join(missing)}")
    df["INCOME"] = df["INCOME_RANGE"].map(range_midpoint)
    df["NET_WORTH_NUM"] = df["NET_WORTH"].map(range_midpoint)
    df["ZIP5"] = zip5(df["ZIP"])
    return df


PARCEL_COLS = [
    "_lead_key", "PARCEL_ID", "PROPERTY_ADDRESS", "OWNER_NAME", "OWNER_MAILING",
    "OWNER_TYPE", "YEAR_BUILT", "LOT_SQFT", "YEARS_OWNED", "LAST_SALE_TYPE",
    "LAST_SALE_PRICE", "EXTRA_FEATURES", "TOTAL_APPRAISAL", "PARCEL_CONFIDENT",
]
PARCEL_DETAIL_COLS = [
    "PARCEL_ID", "PROPERTY_ADDRESS", "OWNER_NAME", "OWNER_MAILING", "YEAR_BUILT",
    "LOT_SQFT", "YEARS_OWNED", "LAST_SALE_TYPE", "LAST_SALE_PRICE", "EXTRA_FEATURES",
    "TOTAL_APPRAISAL",
]
FLAG_COLS = ["PARCEL_CONFIDENT", "HAS_POOL", "HAS_GARAGE", "HAS_OUTBUILDING", "ARMS_LENGTH_SALE"]


def load_property_lookup(path, unit_re=UNIT_ADDRESS_RE):
    """Read the lookup CSV and return ONE parcel row per lead, with a
    PARCEL_CONFIDENT flag.

    Confident = exactly one parcel whose street number, name and suffix agree
    with the lead's address. If several parcels agree, the one whose owner name
    contains the lead's surname wins (only if exactly one does). Parcels on a
    different street suffix (e.g. 'Como St' vs 'Como Dr') are kept but NOT
    confident, and carry no details onto the lead.

    `unit_re` must be the same regex the client passes to enrich_with_parcels(),
    so lead keys line up."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Property lookup file '{path}' not found. Run the lookup step first.")
    lk = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    lk.columns = lk.columns.str.strip()
    missing = [c for c in LOOKUP_REQUIRED_COLS if c not in lk.columns]
    if missing:
        raise KeyError(f"Lookup file is missing columns: {', '.join(missing)}")

    names = lk["LEAD_NAME"].astype("string").fillna("")
    lk["_key"] = lead_key(names, pd.Series("", index=lk.index), lk["LEAD_ADDRESS"], unit_re)

    rows = []
    for key, g in lk.groupby("_key", sort=False):
        g = g[g["Parcel ID"].notna()]
        if g.empty:
            continue  # no parcel found for this lead
        parts = str(g.iloc[0]["LEAD_NAME"]).split()
        first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
        street_ok = g.apply(lambda r: same_street(r["LEAD_ADDRESS"], r["Property Address"]), axis=1)
        cand = g[street_ok]
        if len(cand) == 1:
            row, confident = cand.iloc[0], True
        elif len(cand) > 1:
            named = cand[cand["Owner Name"].map(lambda o: owner_has_surname(o, first, last))]
            row, confident = (named.iloc[0], True) if len(named) == 1 else (cand.iloc[0], False)
        else:
            row, confident = g.iloc[0], False
        rows.append({
            "_lead_key": key,
            "PARCEL_ID": row["Parcel ID"],
            "PROPERTY_ADDRESS": row["Property Address"],
            "OWNER_NAME": row["Owner Name"],
            "OWNER_MAILING": row["Owner Mailing Address"],
            "OWNER_TYPE": owner_type(row["Owner Name"]),
            "YEAR_BUILT": to_num(row["Year Built"]),
            "LOT_SQFT": to_num(row["Land Square Footage"]),
            "YEARS_OWNED": to_num(row["Years Owned"]),
            "LAST_SALE_TYPE": row["Last Sale Type"],
            "LAST_SALE_PRICE": to_num(row["Last Sale Price"]),
            "EXTRA_FEATURES": row["Extra Features"],
            "TOTAL_APPRAISAL": to_num(row.get("Total Appraisal")),
            "PARCEL_CONFIDENT": confident,
        })

    parcels = pd.DataFrame(rows, columns=PARCEL_COLS)
    # A parcel we aren't confident about may be the wrong house, so none of its
    # details (owner, year built...) are carried onto the lead.
    unsure = ~parcels["PARCEL_CONFIDENT"].astype(bool)
    parcels.loc[unsure, PARCEL_DETAIL_COLS] = np.nan
    parcels.loc[unsure, "OWNER_TYPE"] = "unknown"
    features = parcels["EXTRA_FEATURES"].fillna("").str.upper()
    parcels["HAS_POOL"] = features.str.contains("POOL")
    parcels["HAS_GARAGE"] = features.str.contains(GARAGE_RE)
    parcels["HAS_OUTBUILDING"] = features.str.contains(OUTBUILDING_RE)
    parcels["ARMS_LENGTH_SALE"] = (parcels["LAST_SALE_TYPE"].isin(ARMS_LENGTH_TYPES)
                                   & (parcels["LAST_SALE_PRICE"] > 0))
    if parcels["TOTAL_APPRAISAL"].notna().sum() == 0:
        print("note: 'Total Appraisal' is blank for every lookup row.")
    return parcels


# ============================================================
# ENRICH
# ============================================================


def enrich_with_parcels(df, parcels, unit_re=UNIT_ADDRESS_RE):
    """Left-join parcel facts onto the leads (matched on name + street)."""
    df = df.assign(_lead_key=lead_key(df["FIRST_NAME"], df["LAST_NAME"], df["ADDRESS"], unit_re))
    df = df.merge(parcels, on="_lead_key", how="left").drop(columns="_lead_key")
    for col in FLAG_COLS:
        df[col] = df[col].eq(True)       # unmatched leads (NaN) -> False
    df["OWNER_TYPE"] = df["OWNER_TYPE"].fillna("unknown")
    # The owner "looks like the lead" if the surname is in the owner name, the
    # lead's first/last names are swapped, or the owner's mail goes to the property.
    df["OWNER_MATCH"] = [
        owner_match(o, f, l, m, p)
        for o, f, l, m, p in zip(df["OWNER_NAME"], df["FIRST_NAME"], df["LAST_NAME"],
                                 df["OWNER_MAILING"], df["PROPERTY_ADDRESS"])
    ]
    df["OWNER_MATCHES_LEAD"] = df["OWNER_MATCH"].ne("")
    return df


def split_by_exclusion(df, reason_col="EXCLUDE_REASON"):
    """Returns (kept, company_owned, other_excluded) by the client's exclusion reason."""
    company_owned = df[reason_col].eq("company-owned")
    return (df[df[reason_col].eq("")], df[company_owned],
            df[df[reason_col].ne("") & ~company_owned])
