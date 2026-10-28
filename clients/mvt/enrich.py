"""
MVT step 3: grade residential leads for security cameras.

  1. Join each lead to its parcel and check it is the right house (audiencelab.parcels)
  2. Route away leads that shouldn't get the homeowner camera offer
     (company-owned -> landlord list, owner differs from lead -> likely renter).
     Small lots are NOT excluded: townhome owners still buy doorbell / indoor cameras.
  3. Grade on camera-buying signals: just moved in, new construction, property
     crime in their ZIP, home value, big lot / outbuildings / pool, income,
     net worth, contact info
  4. One person per household, rank-based A/B/C tiers, plus a PITCH hint
"""
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from audiencelab.cleaning import zip5
from audiencelab.filtering import dedupe_households, rank_tier, tier_counts
from audiencelab.parcels import (
    enrich_with_parcels, keep_existing, load_property_lookup, load_step1, split_by_exclusion,
)
from clients.mvt import config

KEPT_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "STATE", "ZIP", "PHONE",
             "PHONE_VALIDATED", "EMAIL", "AGE_RANGE", "INCOME_RANGE", "NET_WORTH",
             "PARCEL_ID", "OWNER_NAME", "OWNER_TYPE", "YEAR_BUILT", "LOT_SQFT",
             "YEARS_OWNED", "LAST_SALE_TYPE", "HOME_VALUE", "NEW_CONSTRUCTION",
             "HAS_OUTBUILDING", "HAS_POOL", "LARGE_LOT", "ZIP_CRIME_PCTL",
             "PARCEL_CONFIDENT", "PITCH", "CONSUMER_SCORE", "TIER"]
LANDLORD_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "ZIP", "PHONE", "EMAIL",
                 "OWNER_NAME", "OWNER_MAILING", "YEAR_BUILT", "YEARS_OWNED",
                 "LAST_SALE_TYPE", "ZIP_CRIME_PCTL"]
EXCLUDED_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "ZIP", "PHONE", "EMAIL",
                 "EXCLUDE_REASON", "OWNER_NAME", "OWNER_MAILING", "LOT_SQFT"]


# ============================================================
# CRIME
# ============================================================


def load_crime_by_zip(path):
    """Return a Series ZIP5 -> crime percentile (0-1), or None if no file.

    Accepts raw MPD incidents (property crimes counted by matching the offense
    column) or a pre-aggregated file with PROPERTY_CRIME_COUNT per ZIP (what
    `run.py mvt records crime` writes). Counts are raw, not per household."""
    path = Path(path) if path else None
    if path is None or not path.exists():
        print(f"note: crime file '{path}' not found, so crime points are not used.")
        return None
    cr = pd.read_csv(path, dtype=str, encoding="utf-8-sig", low_memory=False)
    cr.columns = cr.columns.str.strip()
    zip_col = next((c for c in config.CRIME_ZIP_COLS if c in cr.columns), None)
    if zip_col is None:
        raise KeyError(f"Crime file needs a ZIP column (one of: {', '.join(config.CRIME_ZIP_COLS)})")
    cr["ZIP5"] = zip5(cr[zip_col])
    cr = cr[cr["ZIP5"].notna()]

    if "PROPERTY_CRIME_COUNT" in cr.columns:
        counts = cr.groupby("ZIP5")["PROPERTY_CRIME_COUNT"].apply(
            lambda x: pd.to_numeric(x, errors="coerce").sum())
    else:
        type_col = next((c for c in config.CRIME_TYPE_COLS if c in cr.columns), None)
        if type_col is None:
            print("note: no offense/category column in crime file; counting ALL incidents.")
            counts = cr.groupby("ZIP5").size()
        else:
            prop = cr[type_col].astype("string").str.upper().str.contains(config.PROPERTY_CRIME_RE, na=False)
            counts = cr[prop].groupby("ZIP5").size()
    return counts.rank(pct=True)


# ============================================================
# CHECK + SCORE
# ============================================================


def add_camera_fields(df, crime_pct):
    df["LARGE_LOT"] = df["LOT_SQFT"] >= config.LARGE_LOT_SQFT
    df["NEW_CONSTRUCTION"] = df["YEAR_BUILT"] >= date.today().year - 2
    # Home value: appraisal when present, else the price of an arm's-length sale.
    df["HOME_VALUE"] = df["TOTAL_APPRAISAL"].where(
        df["TOTAL_APPRAISAL"].notna(), df["LAST_SALE_PRICE"].where(df["ARMS_LENGTH_SALE"]))
    df["ZIP_CRIME_PCTL"] = df["ZIP5"].map(crime_pct) if crime_pct is not None else np.nan
    return df


def exclusion_reason(df):
    """Why a verified lead should not get the homeowner camera offer ('' = keep)."""
    v = df["PARCEL_CONFIDENT"]
    return pd.Series(np.select(
        [v & df["OWNER_TYPE"].eq("entity"),
         v & df["OWNER_TYPE"].eq("person") & ~df["OWNER_MATCHES_LEAD"]],
        ["company-owned", "owner differs from lead (likely renter)"],
        default=""), index=df.index)


def score(df):
    """Consumer camera grade (max about 100). Property points only count for
    confident parcel matches; crime points use the lead's ZIP, so they count for
    everyone. Move-in timing only counts for arm's-length purchases."""
    P = config.PROPERTY_POINTS
    v = df["PARCEL_CONFIDENT"]
    s = pd.Series(np.where(v, P["verified_base"], P["unverified_base"]), index=df.index, dtype=float)

    arms, owned = v & df["ARMS_LENGTH_SALE"], df["YEARS_OWNED"]
    s += np.where(arms & (owned <= 1), P["moved_in_1yr"],
         np.where(arms & (owned <= 2), P["moved_in_2yr"], 0))
    s += np.where(v & df["NEW_CONSTRUCTION"], P["new_construction"], 0)

    crime = df["ZIP_CRIME_PCTL"]
    s += np.where(crime >= 0.75, P["crime_high"], np.where(crime >= 0.50, P["crime_mid"], 0))

    value = df["HOME_VALUE"]
    s += np.where(v & (value >= config.VALUE_HIGH), P["value_high"],
         np.where(v & (value >= config.VALUE_MID), P["value_mid"], 0))

    s += np.where(v & df["LARGE_LOT"], P["large_lot"], 0)
    s += np.where(v & df["HAS_OUTBUILDING"], P["outbuilding"], 0)
    s += np.where(v & df["HAS_POOL"], P["pool"], 0)

    s += np.where(df["INCOME"] >= 75000, P["income_high"], np.where(df["INCOME"] >= 45000, P["income_mid"], 0))
    s += np.where(df["NET_WORTH_NUM"] >= 100000, P["worth_high"],
                  np.where(df["NET_WORTH_NUM"] >= 25000, P["worth_mid"], 0))
    s += np.where(df["PHONE"].notna() & df["EMAIL"].notna(), P["contact_both"], 0)
    return s


def pitch_hint(df):
    """One-line reason to call, so reps know which angle to open with."""
    hints = []
    for _, r in df.iterrows():
        h = []
        if r["PARCEL_CONFIDENT"] and r["ARMS_LENGTH_SALE"] and r["YEARS_OWNED"] <= 1:
            h.append("new homeowner")
        if r["PARCEL_CONFIDENT"] and r["NEW_CONSTRUCTION"]:
            h.append("new build")
        if r["ZIP_CRIME_PCTL"] >= 0.75:
            h.append("high property-crime ZIP")
        if r["PARCEL_CONFIDENT"] and r["LARGE_LOT"]:
            h.append("large lot / driveway")
        if r["PARCEL_CONFIDENT"] and r["HAS_OUTBUILDING"]:
            h.append("outbuilding")
        hints.append(", ".join(h))
    return hints


# ============================================================
# RUN
# ============================================================


def run(residential_path, lookup_path, crime_path, consumer_path, landlord_path, excluded_path):
    leads = load_step1(residential_path)
    parcels = load_property_lookup(lookup_path, config.ENRICH_UNIT_RE)
    crime_pct = load_crime_by_zip(crime_path)

    df = enrich_with_parcels(leads, parcels, config.ENRICH_UNIT_RE)
    df = add_camera_fields(df, crime_pct)
    df["EXCLUDE_REASON"] = exclusion_reason(df)
    df["CONSUMER_SCORE"] = score(df)
    df["PITCH"] = pitch_hint(df)

    kept, landlords, excluded = split_by_exclusion(df)
    kept, households_merged = dedupe_households(kept, "CONSUMER_SCORE", config.ENRICH_UNIT_RE)
    kept = kept.assign(TIER=rank_tier(kept["CONSUMER_SCORE"], config.TIER_SHARES))

    keep_existing(kept.sort_values("CONSUMER_SCORE", ascending=False), KEPT_COLS).to_csv(
        consumer_path, index=False)
    keep_existing(landlords, LANDLORD_COLS).to_csv(landlord_path, index=False)
    keep_existing(excluded, EXCLUDED_COLS).to_csv(excluded_path, index=False)

    print(f"step 1 residential leads in: {len(df)}")
    print(f"  confident parcel matches: {int(df['PARCEL_CONFIDENT'].sum())}   "
          f"unverified (neutral score): {int((~df['PARCEL_CONFIDENT']).sum())}")
    if crime_pct is not None:
        print(f"  leads in a ZIP with crime data: {int(df['ZIP_CRIME_PCTL'].notna().sum())}")
    print(f"  company-owned -> {landlord_path}: {len(landlords)}")
    print(f"  excluded -> {excluded_path}:",
          excluded["EXCLUDE_REASON"].value_counts().to_dict() or "none")
    print(f"graded leads -> {consumer_path}: {len(kept)} "
          f"(household duplicates merged: {households_merged})")
    print("  tiers:", tier_counts(kept["TIER"]))
    return kept
