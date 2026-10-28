"""
MVT step 1: clean the audience file, pick residential leads and pre-score them.

  1. General cleaning (audiencelab.cleaning)
  2. Service area + contactability (audiencelab.filtering)
  3. Residential: homeowners, no apartments or business addresses
  4. Pre-score from audience data, one person per household, rank-based tiers

Step 3 (clients/mvt/enrich.py) re-grades these with property and crime data.
"""
from collections import Counter

import numpy as np
import pandas as pd

from audiencelab.cleaning import clean_general, load_audience, print_report
from audiencelab.filtering import (
    RESIDENTIAL_REQUIRED_COLS, dedupe_households, filter_leads, rank_tier,
    select_residential, tier_counts,
)
from clients.mvt import config

RES_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "STATE", "ZIP", "PHONE",
            "PHONE_VALIDATED", "EMAIL", "AGE_RANGE", "INCOME_RANGE", "NET_WORTH",
            "APARTMENT", "RESIDENTIAL_SCORE", "TIER"]


def score(df):
    """Homeowners in houses with something worth protecting."""
    s = pd.Series(0, index=df.index, dtype=float)
    s += np.where(df["HOMEOWNER"].eq("Y"), 35, 0)
    s += np.where(df["AGE_LOW"] >= 35, 10, 0)
    s += np.where(df["INCOME"] >= 75000, 15, np.where(df["INCOME"] >= 45000, 8, 0))
    s += np.where(df["NET_WORTH_NUM"] >= 100000, 20, np.where(df["NET_WORTH_NUM"] >= 25000, 10, 0))
    s += np.where(df["PHONE"].notna(), 5, 0) + np.where(df["EMAIL"].notna(), 5, 0)
    return s


def run(audience_path, out_path):
    raw = load_audience(audience_path, extra_required=RESIDENTIAL_REQUIRED_COLS)
    report = Counter()
    df = clean_general(raw, report)
    df = filter_leads(df, config.SERVICE_AREA, report)
    print_report(report)

    df["RESIDENTIAL_SCORE"] = score(df)
    residential, dropped = select_residential(df, drop_po_boxes=config.DROP_PO_BOXES)
    residential, dropped["household duplicates"] = dedupe_households(residential, "RESIDENTIAL_SCORE")
    residential = residential.assign(
        TIER=lambda d: rank_tier(d["RESIDENTIAL_SCORE"], config.TIER_SHARES))

    residential.sort_values("RESIDENTIAL_SCORE", ascending=False)[RES_COLS].to_csv(out_path, index=False)

    print(f"\nraw={len(raw)} cleaned={len(df)}")
    print(f"residential: {len(residential)} (dropped/merged: {dropped}) -> {out_path}")
    print("residential tiers:", tier_counts(residential["TIER"]))
    return residential
