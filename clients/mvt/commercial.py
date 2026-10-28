"""
MVT commercial leads: decision makers at local businesses with premises worth watching.

  1. General cleaning + service area / contactability (shared with residential)
  2. Keep contacts whose company is in the area (audiencelab.commercial)
  3. Score three questions (weights in config.COMMERCIAL_POINTS):
       - does the business have a site that needs cameras?   (industry class)
       - can this person buy?                                (job title)
       - is it local and small enough to decide on site?     (employee count)
     plus property crime in the company ZIP and, after the optional assessor
     lookup, whether the company owns its building
  4. One contact per company (the best one), rank-based A/B/C tiers (top 20% / next 40%), a WHY column

Run:
    python run.py mvt commercial                       # score -> securitycam_commercial_leads.csv
    python run.py mvt commercial-lookup --limit all    # optional: Shelby assessor on company addresses
    python run.py mvt commercial                       # re-score with "owns building" points
"""
from collections import Counter

import numpy as np

from audiencelab import commercial as com
from audiencelab.cleaning import clean_general, load_audience, print_report
from audiencelab.filtering import filter_leads, rank_tier, tier_counts
from clients.mvt import config as C
from clients.mvt.enrich import load_crime_by_zip

OUT_COLS = ["FIRST_NAME", "LAST_NAME", "JOB_TITLE", "BUYER_ROLE", "COMPANY_NAME",
            "COMPANY_INDUSTRY", "INDUSTRY_CLASS", "COMPANY_EMPLOYEE_COUNT", "COMPANY_STREET",
            "COMPANY_CITY", "COMPANY_ZIP5", "COMPANY_PHONE", "COMPANY_DOMAIN", "PHONE",
            "PHONE_VALIDATED", "EMAIL", "ZIP_CRIME_PCTL", "BUILDING", "BUILDING_OWNER",
            "CONTACTS_AT_COMPANY", "WHY", "COMMERCIAL_SCORE", "TIER"]
CLASS_LABELS = {"high": "high-risk premises", "medium": "has premises", "office": "office-only",
                "out": "buys via corporate/specialist"}


def score(df):
    P = C.COMMERCIAL_POINTS
    s = df["INDUSTRY_CLASS"].map({"high": P["industry_high"], "medium": P["industry_medium"],
                                  "office": P["industry_office"], "out": P["industry_out"]}).astype(float)
    s += df["BUYER_ROLE"].map({"owner": P["role_owner"], "operator": P["role_operator"],
                               "senior": P["role_senior"], "staff": P["role_staff"]})
    buyer = df["BUYER_ROLE"].isin(["owner", "operator"])
    s += np.where(buyer & df["INDUSTRY_CLASS"].eq("high"), P["buyer_at_high_risk"], 0)
    s += df["SIZE"].map({"small": P["size_small"], "mid": P["size_mid"], "large": P["size_large"],
                         "enterprise": P["size_enterprise"]}).fillna(0)
    crime = df["ZIP_CRIME_PCTL"]
    s += np.where(crime >= 0.75, P["crime_high"], np.where(crime >= 0.50, P["crime_mid"], 0))
    s += np.where(df["BUILDING"].eq("owns"), P["owns_building"], 0)
    s += np.where(df["PHONE"].notna(), P["direct_phone"], 0)
    return s


def run(audience_path, crime_path, out_path, lookup_input_path, lookup_results_path):
    raw = load_audience(audience_path, extra_required=C.COMMERCIAL_REQUIRED_COLS)
    report = Counter()
    df = clean_general(raw, report)
    df = filter_leads(df, C.SERVICE_AREA, report)
    print_report(report)

    df = com.add_company_fields(com.select_local_companies(df))
    df["BUYER_ROLE"] = [com.buyer_role(t, s, C.COMMERCIAL_ROLES)
                        for t, s in zip(df["JOB_TITLE"], df["SENIORITY_LEVEL"])]
    df["SIZE"] = [com.company_size(c, t, C.FRANCHISE_OWNER_RE, C.EMPLOYEE_SIZE)
                  for c, t in zip(df["COMPANY_EMPLOYEE_COUNT"], df["JOB_TITLE"])]
    df["INDUSTRY_CLASS"] = [com.industry_class(r, sz, C.COMMERCIAL_INDUSTRY)
                            for (_, r), sz in zip(df.iterrows(), df["SIZE"])]

    crime_pct = load_crime_by_zip(crime_path)
    df["ZIP_CRIME_PCTL"] = df["COMPANY_ZIP5"].map(crime_pct) if crime_pct is not None else np.nan
    df = df.join(com.building_status(df, lookup_results_path, "`run.py mvt commercial-lookup`"))

    df["COMMERCIAL_SCORE"] = score(df)
    df["WHY"] = com.why(df, CLASS_LABELS)

    leads = com.dedupe_companies(df, "COMMERCIAL_SCORE")
    leads["TIER"] = rank_tier(leads["COMMERCIAL_SCORE"], C.TIER_SHARES)

    leads[OUT_COLS].to_csv(out_path, index=False)
    com.lookup_input(leads, C.SHELBY_ZIP_PREFIXES).to_csv(lookup_input_path, index=False)

    print(f"\ncommercial contacts in area: {len(df)} -> {len(leads)} companies -> {out_path}")
    print("  industry:", leads["INDUSTRY_CLASS"].value_counts().to_dict())
    print("  buyer role:", leads["BUYER_ROLE"].value_counts().to_dict())
    print("  building:", leads["BUILDING"].replace("", "unknown").value_counts().to_dict())
    print("  tiers:", tier_counts(leads["TIER"]))
    return leads
