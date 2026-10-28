"""
Baltz step 1: clean the audience file and pick residential leads.

  1. General cleaning (audiencelab.cleaning)
  2. Service area + contactability (audiencelab.filtering)
  3. Residential: homeowners, no apartments, PO boxes or business addresses

Leads are NOT graded here; step 3 (clients/baltz/enrich.py) grades them after
the Shelby lookup.
"""
from collections import Counter

from audiencelab.cleaning import clean_general, load_audience, print_report
from audiencelab.filtering import RESIDENTIAL_REQUIRED_COLS, filter_leads, select_residential
from clients.baltz import config

RES_COLS = ["FIRST_NAME", "LAST_NAME", "ADDRESS", "CITY", "STATE", "ZIP", "PHONE",
            "PHONE_VALIDATED", "EMAIL", "AGE_RANGE", "INCOME_RANGE", "NET_WORTH",
            "APARTMENT"]


def run(audience_path, out_path):
    raw = load_audience(audience_path, extra_required=RESIDENTIAL_REQUIRED_COLS)
    report = Counter()
    df = clean_general(raw, report)
    df = filter_leads(df, config.SERVICE_AREA, report)
    print_report(report)

    residential, dropped = select_residential(df, drop_po_boxes=config.DROP_PO_BOXES)
    residential[RES_COLS].to_csv(out_path, index=False)

    print(f"\nraw={len(raw)} cleaned={len(df)}")
    print(f"residential: {len(residential)} (dropped: {dropped}) -> {out_path}")
    print("not graded yet: run the lookup and enrich steps next")
    return residential
