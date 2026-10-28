"""
audiencelab.filtering
---------------------
Lead filtering shared by every client, run on the output of
cleaning.clean_general():

  1. Service area: flag whether the home / company is in the client's area
  2. Contactability: drop rows with no usable phone or email
  3. Residential selection: homeowners at a home address in the area, minus
     apartments/units (and optionally PO boxes)
  4. Household de-duplication and rank-based A/B/C tiers (the only grading used)

Each client passes its own ServiceArea and options; nothing here is scored.
"""
from dataclasses import dataclass

import numpy as np
import pandas as pd

from audiencelab.cleaning import normalize_city, zip5

# ============================================================
# CONFIG
# ============================================================

# Apartments / units are excluded from residential leads, even when HOMEOWNER = Y.
UNIT_ADDRESS_RE = r"(?:\b(?:apt|apartment|unit|lot)\b\.?\s*#?\s*\w+|#\s*\w+)"

# PO boxes aren't physical homes.
PO_BOX_RE = r"\b(?:p\.?\s?o\.?\s*box|post\s+office\s+box)\b"

# Columns the residential steps need beyond cleaning.GENERAL_REQUIRED_COLS.
RESIDENTIAL_REQUIRED_COLS = ["HOMEOWNER"]


@dataclass(frozen=True)
class ServiceArea:
    states: frozenset
    cities: frozenset          # lower-case, after cleaning.normalize_city()
    zip_prefixes: tuple = ()   # ZIPs catch people whose city is blank or misspelled
    zips: frozenset = frozenset()


# Memphis metro. Verify against each client's real service map before relying on it.
MEMPHIS_METRO = ServiceArea(
    states=frozenset({"TN", "MS", "AR"}),
    cities=frozenset({
        "memphis", "bartlett", "cordova", "collierville", "germantown", "arlington",
        "lakeland", "millington", "southaven", "olive branch", "horn lake", "west memphis",
    }),
    zip_prefixes=("381",),          # Memphis + Bartlett/Germantown/Cordova parts
    zips=frozenset({
        "38002",                    # Arlington / Lakeland
        "38016", "38018",           # Cordova
        "38017",                    # Collierville
        "38053", "38054",           # Millington
        "38671", "38672",           # Southaven
        "38654",                    # Olive Branch
        "38637",                    # Horn Lake
        "72301", "72303",           # West Memphis
    }),
)


# ============================================================
# SERVICE AREA + CONTACTABILITY
# ============================================================


def in_service_area(city, state, zip_code, area: ServiceArea):
    city_ok = normalize_city(city).isin(area.cities)
    zip_ok = zip_code.isin(area.zips) | zip_code.str[:3].isin(area.zip_prefixes)
    state_ok = state.isin(area.states) | state.isna()  # ZIP/city still has to match
    return ((city_ok & state.isin(area.states)) | (zip_ok & state_ok)).fillna(False)


def filter_leads(df, area: ServiceArea, report):
    """Flag service-area membership and drop rows that are out of area or have
    no usable phone/email. Expects the output of clean_general().

    Adds: IN_AREA_HOME, IN_AREA_COMPANY, IN_SERVICE_AREA, APARTMENT, PO_BOX."""
    df = df.copy()
    df["IN_AREA_HOME"] = in_service_area(df["CITY"], df["STATE"], df["ZIP"], area)
    blank = pd.Series(np.nan, index=df.index, dtype=object)
    df["IN_AREA_COMPANY"] = in_service_area(
        df.get("COMPANY_CITY", blank), df.get("COMPANY_STATE", blank),
        zip5(df.get("COMPANY_ZIP", blank)), area)
    df["IN_SERVICE_AREA"] = df["IN_AREA_HOME"] | df["IN_AREA_COMPANY"]

    address = df["ADDRESS"].astype("string")
    df["APARTMENT"] = address.str.contains(UNIT_ADDRESS_RE, case=False, regex=True).fillna(False)
    df["PO_BOX"] = address.str.contains(PO_BOX_RE, case=False, regex=True).fillna(False)

    contactable = df["PHONE"].notna() | df["EMAIL"].notna()
    report["outside service area"] = int((~df["IN_SERVICE_AREA"]).sum())
    report["in area, no usable phone/email"] = int((df["IN_SERVICE_AREA"] & ~contactable).sum())
    df = df[df["IN_SERVICE_AREA"] & contactable]
    report["kept"] = len(df)
    return df


def select_residential(df, drop_po_boxes=False):
    """Homeowners whose home is in the area at a non-business address, minus
    apartments (and PO boxes if asked). Returns (leads, counts of what was dropped)."""
    homeowner = df["HOMEOWNER"].eq("Y")
    stats = {"business addresses": int((~df["IS_RESIDENTIAL"] & homeowner).sum())}
    res = df[homeowner & df["IN_AREA_HOME"] & df["IS_RESIDENTIAL"]]
    stats["apartments"] = int(res["APARTMENT"].sum())
    res = res[~res["APARTMENT"]]
    if drop_po_boxes:
        stats["PO boxes"] = int(res["PO_BOX"].sum())
        res = res[~res["PO_BOX"]]
    return res, stats


# ============================================================
# HOUSEHOLDS + TIERS
# ============================================================


def street_key(address, unit_re=UNIT_ADDRESS_RE):
    """'3559 Josee Ln Apt O7' and '3559 JOSEE LN' -> '3559 josee ln' for household matching."""
    s = address.astype("string").str.lower()
    s = s.str.replace(r"[,.]", " ", regex=True).str.replace(unit_re, "", regex=True)
    return s.str.replace(r"\s+", " ", regex=True).str.strip()


def dedupe_households(df, score_col, unit_re=UNIT_ADDRESS_RE):
    """One mailer/call per household: same street (unit ignored) + same phone,
    or same full address. Keeps the highest-scoring person.
    Returns (deduped, number merged)."""
    df = df.sort_values(score_col, ascending=False)
    street = street_key(df["ADDRESS"], unit_re)
    by_phone = (street + "|" + df["PHONE"].astype("string")).duplicated() & df["PHONE"].notna()
    by_addr = (df["ADDRESS"].astype("string").str.lower() + "|" + df["ZIP"].astype("string")).duplicated()
    return df[~(by_phone.fillna(False) | by_addr.fillna(False))], int((by_phone | by_addr).sum())


def rank_tier(score, shares):
    """Rank-based tiers, e.g. shares={"A": 0.20, "B": 0.40}: top 20% = A, next
    40% = B, rest = C. Tied scores land in the same tier, and a tied block only
    gets the higher tier if the whole block fits inside it."""
    pct = score.rank(ascending=False, method="max", pct=True)
    a_cut = shares["A"]
    b_cut = a_cut + shares["B"]
    return pd.Series(np.select([pct <= a_cut, pct <= b_cut], ["A", "B"], "C"), index=score.index)


def tier_counts(tiers):
    return tiers.value_counts().reindex(list("ABC"), fill_value=0).to_dict()
