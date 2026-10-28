"""
Baltz commercial leads, in two lists (each graded A/B/C by rank, like the old list):

  commercial_leads.csv          DIRECT: decision makers over a lot of pavement (property
                                managers, HOAs, apartments, churches, warehouses, dealers...).
                                Government stays here as LEAD_TYPE "public bid", scored a
                                little lower than private property.
  commercial_partner_leads.csv  PARTNERS: GCs / builders / developers (sub out concrete) and
                                architects / civil engineers / landscapers (specify it).

Concrete / paving companies are competitors and are dropped. Weights and patterns
are in config.py (COMMERCIAL section).

Run:
    python run.py baltz commercial
    python run.py baltz commercial-lookup --limit all   # optional: who owns each building,
    python run.py baltz commercial                      #   lot size, year built -> re-score
"""
from collections import Counter

import numpy as np
from audiencelab import commercial as com
from audiencelab.cleaning import clean_general, load_audience, print_report
from audiencelab.filtering import filter_leads, rank_tier, tier_counts
from clients.baltz import config as C

DIRECT_COLS = ["FIRST_NAME", "LAST_NAME", "JOB_TITLE", "BUYER_ROLE", "COMPANY_NAME",
               "COMPANY_INDUSTRY", "LEAD_TYPE", "INDUSTRY_CLASS", "COMPANY_EMPLOYEE_COUNT",
               "COMPANY_STREET", "COMPANY_CITY", "COMPANY_ZIP5", "COMPANY_PHONE", "COMPANY_DOMAIN",
               "PHONE", "PHONE_VALIDATED", "EMAIL", "BUILDING", "BUILDING_OWNER", "PARCEL_LOT_SQFT",
               "PARCEL_YEAR_BUILT", "CONTACTS_AT_COMPANY", "WHY", "COMMERCIAL_SCORE", "TIER"]
PARTNER_COLS = ["FIRST_NAME", "LAST_NAME", "JOB_TITLE", "BUYER_ROLE", "COMPANY_NAME",
                "COMPANY_INDUSTRY", "PARTNER_TYPE", "COMPANY_EMPLOYEE_COUNT", "COMPANY_STREET",
                "COMPANY_CITY", "COMPANY_ZIP5", "COMPANY_PHONE", "COMPANY_DOMAIN", "PHONE",
                "PHONE_VALIDATED", "EMAIL", "CONTACTS_AT_COMPANY", "WHY", "PARTNER_SCORE", "TIER"]

DIRECT_LABELS = {"high": "lots of flatwork", "public_bid": "PUBLIC BID", "medium": "has a site",
                 "office": "office-only tenant", "out": "buys via corporate facilities"}
PARTNER_LABELS = {"partner_gc": "GC/builder/developer (subs concrete)",
                  "partner_design": "architect/engineer/landscaper (specs concrete)"}
PARTNER_CLASSES = set(PARTNER_LABELS)


# ============================================================
# SCORE
# ============================================================


def score_direct(df):
    P = C.DIRECT_POINTS
    cls = df["INDUSTRY_CLASS"]
    s = cls.map({"high": P["industry_high"], "public_bid": P["industry_public_bid"],
                 "medium": P["industry_medium"], "office": P["industry_office"],
                 "out": P["industry_out"]}).astype(float)
    s += df["BUYER_ROLE"].map({"owner": P["role_owner"], "operator": P["role_operator"],
                               "senior": P["role_senior"], "staff": P["role_staff"]})
    buyer = df["BUYER_ROLE"].isin(["owner", "operator"])
    s += np.where(buyer & cls.isin(["high", "public_bid"]), P["buyer_at_flatwork"], 0)
    s += df["SIZE"].map({"small": P["size_small"], "mid": P["size_mid"], "large": P["size_large"],
                   "enterprise": P["size_enterprise"]}).fillna(0)
    s += np.where(df["BUILDING"].eq("owns"), P["owns_building"],
                  np.where(df["BUILDING"].eq("leases"), P["leases_building"], 0))
    s += np.where(df["BIG_LOT"], P["big_lot"], 0)
    s += np.where(df["OLD_BUILDING"], P["built_pre_1995"], 0)
    s += np.where(df["PHONE"].notna(), P["direct_phone"], 0)
    return s


def score_partner(df):
    P = C.PARTNER_POINTS
    s = df["INDUSTRY_CLASS"].map({"partner_gc": P["type_gc"], "partner_design": P["type_design"]}).astype(float)
    s += df["BUYER_ROLE"].map({"owner": P["role_owner"], "operator": P["role_operator"],
                               "senior": P["role_senior"], "staff": P["role_staff"]})
    count = df["COMPANY_EMPLOYEE_COUNT"].astype("string").str.strip().str.lower()
    s += count.map(C.PARTNER_SIZE_POINTS).astype(float).fillna(0)
    s += np.where(df["PHONE"].notna(), P["direct_phone"], 0)
    return s


# ============================================================
# RUN
# ============================================================


def run(audience_path, direct_path, partner_path, lookup_input_path, lookup_results_path):
    raw = load_audience(audience_path, extra_required=C.COMMERCIAL_REQUIRED_COLS)
    report = Counter()
    df = clean_general(raw, report)
    df = filter_leads(df, C.SERVICE_AREA, report)
    print_report(report)

    df = com.add_company_fields(com.select_local_companies(df))
    df["SIZE"] = [com.company_size(c, t, C.FRANCHISE_OWNER_RE)
                  for c, t in zip(df["COMPANY_EMPLOYEE_COUNT"], df["JOB_TITLE"])]
    df["INDUSTRY_CLASS"] = [com.industry_class(r, sz, C.COMMERCIAL_INDUSTRY)
                            for (_, r), sz in zip(df.iterrows(), df["SIZE"])]

    competitors = df["INDUSTRY_CLASS"].eq("competitor")
    is_partner = df["INDUSTRY_CLASS"].isin(PARTNER_CLASSES)
    direct, partners = df[~competitors & ~is_partner].copy(), df[is_partner].copy()

    # ---- direct ----
    direct["BUYER_ROLE"] = [com.buyer_role(t, s, C.DIRECT_ROLES)
                            for t, s in zip(direct["JOB_TITLE"], direct["SENIORITY_LEVEL"])]
    direct["LEAD_TYPE"] = np.where(direct["INDUSTRY_CLASS"].eq("public_bid"), "public bid", "direct")
    # An agency's head count says little about who decides; public bids always score as "mid".
    direct["SIZE"] = direct["SIZE"].where(direct["LEAD_TYPE"].ne("public bid"), "mid")
    direct = direct.join(com.building_status(direct, lookup_results_path, "`run.py baltz commercial-lookup`"))
    direct["BIG_LOT"] = direct["PARCEL_LOT_SQFT"] >= C.BIG_LOT_SQFT
    direct["OLD_BUILDING"] = direct["PARCEL_YEAR_BUILT"] < C.OLD_BUILDING_YEAR
    direct["COMMERCIAL_SCORE"] = score_direct(direct)
    direct["WHY"] = com.why(direct, DIRECT_LABELS,
                            extras=[("BIG_LOT", "1+ acre lot"), ("OLD_BUILDING", "built pre-1995")])
    direct = com.dedupe_companies(direct, "COMMERCIAL_SCORE")
    direct["TIER"] = rank_tier(direct["COMMERCIAL_SCORE"], C.TIER_SHARES)

    # ---- partners ----
    partners["BUYER_ROLE"] = [com.buyer_role(t, s, C.PARTNER_ROLES)
                              for t, s in zip(partners["JOB_TITLE"], partners["SENIORITY_LEVEL"])]
    partners["PARTNER_TYPE"] = partners["INDUSTRY_CLASS"].map(
        {"partner_gc": "GC / builder / developer", "partner_design": "architect / engineer / landscaper"})
    partners["PARTNER_SCORE"] = score_partner(partners)
    partners["WHY"] = com.why(partners, PARTNER_LABELS)
    partners = com.dedupe_companies(partners, "PARTNER_SCORE")
    partners["TIER"] = rank_tier(partners["PARTNER_SCORE"], C.TIER_SHARES)

    direct[DIRECT_COLS].to_csv(direct_path, index=False)
    partners[PARTNER_COLS].to_csv(partner_path, index=False)
    com.lookup_input(direct).to_csv(lookup_input_path, index=False)

    print(f"\ncommercial contacts in area: {len(df)} "
          f"(competitors dropped: {int(competitors.sum())})")
    print(f"direct -> {direct_path}: {len(direct)} companies "
          f"({int(direct['LEAD_TYPE'].eq('public bid').sum())} public bid)")
    print("  industry:", direct["INDUSTRY_CLASS"].value_counts().to_dict())
    print("  building:", direct["BUILDING"].replace("", "unknown").value_counts().to_dict())
    print("  tiers:", tier_counts(direct["TIER"]))
    print(f"partners -> {partner_path}: {len(partners)} companies",
          partners["PARTNER_TYPE"].value_counts().to_dict())
    print("  tiers:", tier_counts(partners["TIER"]))
    return direct, partners
