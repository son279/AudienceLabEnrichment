"""
Baltz step 3: grade residential leads with Shelby County property data.

  1. Join each lead to its parcel and check it is the right house (audiencelab.parcels)
  2. Route away leads that shouldn't get the homeowner concrete offer
     (company-owned -> investor list, owner differs, tiny lot / townhome)
  3. Grade the rest: pool, purchase timing, home age, lot size, income,
     net worth, contact info
  4. One person per household, rank-based A/B/C tiers

Also defines `lookup_signals`, the concrete-work hints written into the
Signals column of the Shelby lookup (step 2).
"""
from datetime import date

import numpy as np
import pandas as pd

from audiencelab.filtering import UNIT_ADDRESS_RE, dedupe_households, rank_tier, tier_counts
from audiencelab.parcels import (
    enrich_with_parcels, keep_existing, load_property_lookup, load_step1, split_by_exclusion,
)
from audiencelab.shelby_lookup import PERMIT_REASONS
from clients.baltz import config

KEPT_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "STATE", "ZIP", "PHONE",
             "PHONE_VALIDATED", "EMAIL", "AGE_RANGE", "INCOME_RANGE", "NET_WORTH",
             "PARCEL_ID", "OWNER_NAME", "OWNER_TYPE", "OWNER_MATCH", "YEAR_BUILT", "LOT_SQFT",
             "YEARS_OWNED", "LAST_SALE_TYPE", "HAS_POOL", "HAS_GARAGE", "LARGE_LOT",
             "PARCEL_CONFIDENT", "RESIDENTIAL_SCORE", "TIER"]
INVESTOR_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "ZIP", "PHONE", "EMAIL",
                 "OWNER_NAME", "OWNER_MAILING", "YEAR_BUILT", "YEARS_OWNED", "LAST_SALE_TYPE"]
EXCLUDED_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "ZIP", "PHONE", "EMAIL",
                 "EXCLUDE_REASON", "OWNER_NAME", "OWNER_MATCH", "OWNER_MAILING", "LOT_SQFT"]


# ============================================================
# STEP 2 HOOK: Signals column of the Shelby lookup
# ============================================================


def lookup_signals(data, year_built, permits):
    """Hints that a homeowner may need concrete / exterior work."""
    today = date.today()
    out = []
    owned = data.get("Years Owned")
    if owned is not None and owned <= 2:
        out.append("Recent buyer (<=2 yrs): likely to start projects")
    if owned is not None and owned >= 15:
        out.append("Long-time owner (15+ yrs): aging driveway/patio")
    since = data.get("Years Since Permit")
    if since is not None and since <= 3:
        out.append("Recent permit (<=3 yrs): active remodeling")
    if year_built.isdigit() and today.year - int(year_built) >= 30 and \
            (since is None or since >= 10):
        out.append("Older home, no recent permits: deferred upgrades")
    features = data.get("Extra Features", "").upper()
    if "POOL" in features:
        out.append("Has pool: deck/patio work")
    reasons = [PERMIT_REASONS.get(r.upper(), r).upper() for _, _, r in permits]
    if any("POOL" in r or "DECK" in r or "DRIVEWAY" in r for r in reasons):
        out.append("Past outdoor/concrete-related permit")
    return out


# ============================================================
# CHECK + SCORE
# ============================================================


def exclusion_reason(df):
    """Why a verified lead should not get the homeowner concrete offer ('' = keep).
    Only applied to confident parcel matches; unverified leads are never excluded here."""
    v = df["PARCEL_CONFIDENT"]
    return pd.Series(np.select(
        [v & df["OWNER_TYPE"].eq("entity"),
         v & df["OWNER_TYPE"].eq("person") & ~df["OWNER_MATCHES_LEAD"],
         v & (df["LOT_SQFT"] < config.MIN_LOT_SQFT)],
        ["company-owned", "owner differs from lead", "tiny lot (townhome/PUD)"],
        default=""), index=df.index)


def score(df):
    """The residential grade (max about 100). Property points only count for
    confident parcel matches. Years owned is only trusted when the last sale was
    an arm's-length purchase."""
    P = config.PROPERTY_POINTS
    v = df["PARCEL_CONFIDENT"]
    s = pd.Series(np.where(v, P["verified_base"], P["unverified_base"]), index=df.index, dtype=float)

    s += np.where(v & df["HAS_POOL"], P["pool"], np.where(v & df["HAS_GARAGE"], P["garage"], 0))

    arms, owned = v & df["ARMS_LENGTH_SALE"], df["YEARS_OWNED"]
    s += np.where(arms & (owned <= 2), P["recent_buyer"],
         np.where(arms & (owned <= 5), P["buyer_2_to_5"],
         np.where(arms & (owned >= 15), P["long_owner"], 0)))

    built = df["YEAR_BUILT"]
    s += np.where(v & (built < 1995), P["built_pre_1995"],
         np.where(v & (built < 2005), P["built_1995_2004"], 0))

    lot = df["LOT_SQFT"]
    s += np.where(v & lot.between(8_000, 25_000), P["lot_prime"],
         np.where(v & lot.between(config.MIN_LOT_SQFT, 8_000), P["lot_small"], 0))

    s += np.where(df["INCOME"] >= 75000, P["income_high"], np.where(df["INCOME"] >= 45000, P["income_mid"], 0))
    s += np.where(df["NET_WORTH_NUM"] >= 100000, P["worth_high"],
                  np.where(df["NET_WORTH_NUM"] >= 25000, P["worth_mid"], 0))
    s += np.where(df["PHONE"].notna() & df["EMAIL"].notna(), P["contact_both"], 0)
    return s


# ============================================================
# RUN
# ============================================================


def run(residential_path, lookup_path, enriched_path, investors_path, excluded_path):
    leads = load_step1(residential_path)
    parcels = load_property_lookup(lookup_path, UNIT_ADDRESS_RE)

    df = enrich_with_parcels(leads, parcels, UNIT_ADDRESS_RE)
    df["LARGE_LOT"] = df["LOT_SQFT"] > config.LARGE_LOT_SQFT
    df["EXCLUDE_REASON"] = exclusion_reason(df)
    df["RESIDENTIAL_SCORE"] = score(df)

    kept, investors, excluded = split_by_exclusion(df)
    kept, households_merged = dedupe_households(kept, "RESIDENTIAL_SCORE", UNIT_ADDRESS_RE)
    kept = kept.assign(TIER=rank_tier(kept["RESIDENTIAL_SCORE"], config.TIER_SHARES))

    keep_existing(kept.sort_values("RESIDENTIAL_SCORE", ascending=False), KEPT_COLS).to_csv(
        enriched_path, index=False)
    keep_existing(investors, INVESTOR_COLS).to_csv(investors_path, index=False)
    keep_existing(excluded, EXCLUDED_COLS).to_csv(excluded_path, index=False)

    print(f"step 1 residential leads in: {len(df)}")
    print(f"  confident parcel matches: {int(df['PARCEL_CONFIDENT'].sum())}   "
          f"unverified (neutral score): {int((~df['PARCEL_CONFIDENT']).sum())}")
    print(f"  company-owned -> {investors_path}: {len(investors)}")
    print(f"  excluded -> {excluded_path}:",
          excluded["EXCLUDE_REASON"].value_counts().to_dict() or "none")
    print(f"graded leads -> {enriched_path}: {len(kept)} "
          f"(household duplicates merged: {households_merged})")
    print("  tiers:", tier_counts(kept["TIER"]))
    return kept
