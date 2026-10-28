"""
audiencelab.commercial
----------------------
Commercial-lead machinery shared by every client. A client supplies its own
rules (IndustryRules / RoleRules, weights) and scoring; this module does the
mechanics:

  select_local_companies()  contacts whose company is in the area (or, if the
                            company has no location, whose home is)
  add_company_fields()      COMPANY_STREET, COMPANY_ZIP5, COMPANY_KEY
  industry_class()          class from company name -> industry -> NAICS -> description
  buyer_role()              owner / operator / senior / staff from the job title
  company_size()            small / mid / large / enterprise / unknown
  lookup_input()            Shelby company addresses for audiencelab.shelby_lookup
  building_status()         owns / leases + lot size and year built from that lookup
  dedupe_companies()        one contact per company (the best one)
  why()                     one-line reason per lead
"""
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from audiencelab.cleaning import smart_title, zip5
from audiencelab.parcels import owner_has_surname, same_street, to_num

EMPLOYEE_SIZE = {
    "1 to 10": "small", "11 to 25": "small", "26 to 50": "small",
    "51 to 100": "mid", "101 to 250": "mid", "251 to 500": "mid",
    "501 to 1000": "large", "1001 to 5000": "large",
    "5001 to 10000": "enterprise", "10000+": "enterprise",
}
SHELBY_ZIP_PREFIXES = ("380", "381")   # the assessor lookup only covers Shelby County

_COMPANY_STOPWORDS = {"THE", "AND", "OF", "INC", "LLC", "LLP", "LP", "PLLC", "PLC", "CO", "CORP",
                      "CORPORATION", "COMPANY", "GROUP", "SERVICES", "SERVICE", "INTERNATIONAL",
                      "ENTERPRISES", "HOLDINGS", "ASSOCIATES", "PARTNERS", "MEMPHIS", "TN"}


@dataclass
class IndustryRules:
    # (label, class, regex) in priority order; at each level the first match wins.
    classes: list
    # Labels a company NAME alone may decide (vague names fall through to the industry).
    name_labels: set
    naics: dict = field(default_factory=dict)       # NAICS prefix -> class (longest wins)
    name_skip_re: str = r"$^"                       # names that never decide (associations...)
    default: str = "office"
    # LinkedIn files clinics under "Hospitals And Health Care": if set, a SMALL business
    # whose industry says hospital gets this class instead of the hospital's class.
    small_hospital_class: str = None
    hospital_class: str = "out"
    # If set to (from_classes, to_class, regex): a description matching regex upgrades
    # a lead classed as one of from_classes (e.g. a "real estate" firm that manages property).
    description_upgrade: tuple = None


@dataclass
class RoleRules:
    roles: list                     # (role, regex) in priority order, matched on the job title
    senior_first_re: str = r"\bvice[- ]president\b|\bvp\b|\bassistant\b|\bdeputy\b"
    seniority: set = field(default_factory=lambda: {"cxo", "vp", "director", "manager", "owner", "partner"})
    senior_title_re: str = r"\bpartner\b|\bdirector\b|\bmanager\b|\bhead\b|\bchief\b"
    # Roles a senior_first_re title ("Vice President ...") can never get. None = all roles.
    senior_first_blocks: set = None


# ============================================================
# FIELDS
# ============================================================


def company_street(value):
    """COMPANY_ADDRESS is plain text or a JSON blob
    ('{"street1": "65 union ave", "street2": "suite 500"}'). Returns 'street [unit]'."""
    if not isinstance(value, str) or not value.strip():
        return np.nan
    v = value.strip()
    if v.startswith("{"):
        try:
            d = json.loads(v)
            v = " ".join(str(d.get(k) or "").strip() for k in ("street1", "street2")).strip()
        except ValueError:
            return np.nan
    # Needs a street number to be an address ("united states" / "panama city, florida" aren't).
    return v if re.match(r"^\d+\s+\w", v) else np.nan


def company_key(df):
    name = df["COMPANY_NAME"].astype("string").str.upper().str.replace(r"[^A-Z0-9 ]", " ", regex=True)
    name = name.str.replace(r"\s+", " ", regex=True).str.strip()
    street = df["COMPANY_STREET"].astype("string").str.lower().str.extract(r"^(\d+\s+\S+)")[0]
    return (name + "|" + street.fillna("")).astype(object)


def select_local_companies(df):
    """Company must be local; if its location is unknown, fall back to where the contact lives."""
    company_located = df["COMPANY_CITY"].notna() | df["COMPANY_STATE"].notna()
    keep = df["COMPANY_NAME"].notna() & (df["IN_AREA_COMPANY"] | (~company_located & df["IN_AREA_HOME"]))
    return df[keep].copy()


def add_company_fields(df):
    company_located = df["COMPANY_CITY"].notna() | df["COMPANY_STATE"].notna()
    df["COMPANY_STREET"] = smart_title(df["COMPANY_ADDRESS"].map(company_street))
    df["COMPANY_ZIP5"] = zip5(df["COMPANY_ZIP"]).where(company_located, df["ZIP"])
    df["COMPANY_KEY"] = company_key(df)
    return df


# ============================================================
# CLASSIFY
# ============================================================


def _label_of(text, rules: IndustryRules):
    if not isinstance(text, str) or not text.strip():
        return None, None
    t = text.lower()
    for label, cls, pattern in rules.classes:
        if re.search(pattern, t):
            return label, cls
    return None, None


def _naics_class(code, naics):
    if not isinstance(code, str):
        return None
    for c in (x.strip() for x in code.split(",")):
        for n in range(len(c), 1, -1):
            if c[:n] in naics:
                return naics[c[:n]]
    return None


def industry_class(row, size, rules: IndustryRules):
    """Class from company name -> industry -> NAICS -> description (never the job
    title, so a 'Director of Pharmacy' at a hospital doesn't count as a pharmacy)."""
    name = row.get("COMPANY_NAME")
    skip_name = isinstance(name, str) and re.search(rules.name_skip_re, name.lower())
    result = None
    if not skip_name:
        label, cls = _label_of(name, rules)
        if label in rules.name_labels:
            result = cls
    if result is None:
        industry = row.get("COMPANY_INDUSTRY")
        _, by_industry = _label_of(industry, rules)
        if (rules.small_hospital_class and by_industry == rules.hospital_class and size == "small"
                and isinstance(industry, str) and "hospital" in industry.lower()):
            by_industry = rules.small_hospital_class
        by_naics = by_industry or _naics_class(row.get("COMPANY_NAICS"), rules.naics)
        if by_naics or skip_name:      # an association with no industry stays default
            result = by_naics or rules.default
        else:
            result = _label_of(row.get("COMPANY_DESCRIPTION"), rules)[1] or rules.default
    if rules.description_upgrade:
        from_classes, to_class, pattern = rules.description_upgrade
        desc = row.get("COMPANY_DESCRIPTION")
        if result in from_classes and isinstance(desc, str) and re.search(pattern, desc.lower()):
            result = to_class
    return result


def buyer_role(title, seniority, rules: RoleRules):
    """First matching role from the job title, else 'senior' / 'staff' from seniority."""
    t = title.lower() if isinstance(title, str) else ""
    senior_first = bool(t) and bool(re.search(rules.senior_first_re, t))
    if t:
        for role, pattern in rules.roles:
            blocked = senior_first and (rules.senior_first_blocks is None or role in rules.senior_first_blocks)
            if not blocked and re.search(pattern, t):
                return role
    s = seniority.lower() if isinstance(seniority, str) else ""
    if s in rules.seniority or re.search(rules.senior_title_re, t):
        return "senior"
    return "staff"


def company_size(count, title, franchise_owner_re=r"\bowner\b|\bco-?owner\b|\bfranchisee\b|\bproprietor\b",
                 size_map=EMPLOYEE_SIZE):
    """Owners of a big brand are usually franchisees, so they count as small."""
    if isinstance(title, str) and re.search(franchise_owner_re, title.lower()):
        return "small"
    return size_map.get(str(count).strip().lower() if isinstance(count, str) else "", "unknown")


# ============================================================
# BUILDING (optional Shelby assessor lookup on the company address)
# ============================================================


def lookup_input(df, zip_prefixes=SHELBY_ZIP_PREFIXES):
    """One row per Shelby company address, in the shape audiencelab.shelby_lookup reads."""
    shelby = (df["COMPANY_ZIP5"].astype("string").str[:3].isin(zip_prefixes)
              | (df["COMPANY_ZIP5"].isna() & df["COMPANY_STATE"].eq("TN")))
    rows = df[shelby & df["COMPANY_STREET"].notna()]
    out = pd.DataFrame({"FIRST_NAME": rows["COMPANY_NAME"], "LAST_NAME": "",
                        "ADDRESS": rows["COMPANY_STREET"], "COMPANY_KEY": rows["COMPANY_KEY"]})
    return out.drop_duplicates("COMPANY_KEY")


def _company_words(name):
    words = re.sub(r"[^A-Z0-9 ]", " ", str(name).upper()).split()
    return {w for w in words if len(w) >= 3 and w not in _COMPANY_STOPWORDS}


def building_status(df, lookup_path, run_hint="commercial-lookup"):
    """Returns a frame with BUILDING ('owns' / 'leases' / ''), BUILDING_OWNER,
    PARCEL_LOT_SQFT and PARCEL_YEAR_BUILT. 'owns' = the parcel's single owner shares
    a distinctive word with the company name or carries the contact's surname."""
    out = pd.DataFrame({"BUILDING": "", "BUILDING_OWNER": np.nan,
                        "PARCEL_LOT_SQFT": np.nan, "PARCEL_YEAR_BUILT": np.nan}, index=df.index)
    out["BUILDING_OWNER"] = out["BUILDING_OWNER"].astype(object)
    path = Path(lookup_path)
    if not path.exists():
        print(f"note: '{path.name}' not found, so no building points "
              f"(run the {run_hint} step to add them).")
        return out
    lk = pd.read_csv(path, dtype=str, encoding="utf-8-sig")
    lk = lk[lk["Parcel ID"].notna()]
    parcels = {}
    for (name, addr), g in lk.groupby(["LEAD_NAME", "LEAD_ADDRESS"], sort=False):
        same = g[g.apply(lambda r: same_street(r["LEAD_ADDRESS"], r["Property Address"]), axis=1)]
        owners = same["Owner Name"].dropna()
        if len(owners.unique()) == 1:   # one owner for that address
            first = same[same["Owner Name"].notna()].iloc[0]
            parcels[(str(name).strip(), str(addr).strip())] = (
                first["Owner Name"], to_num(first.get("Land Square Footage")), to_num(first.get("Year Built")))
    for i, r in df.iterrows():
        hit = parcels.get((str(r["COMPANY_NAME"]).strip(), str(r["COMPANY_STREET"]).strip()))
        if hit is None:
            continue
        owner, lot, built = hit
        owner_words = set(re.sub(r"[^A-Z0-9 ]", " ", owner.upper()).split())
        ties = (_company_words(r["COMPANY_NAME"]) & owner_words
                or owner_has_surname(owner, r["FIRST_NAME"], r["LAST_NAME"]))
        out.at[i, "BUILDING"] = "owns" if ties else "leases"
        out.at[i, "BUILDING_OWNER"] = owner
        out.at[i, "PARCEL_LOT_SQFT"] = lot
        out.at[i, "PARCEL_YEAR_BUILT"] = built
    return out


# ============================================================
# OUTPUT HELPERS
# ============================================================


def dedupe_companies(df, score_col):
    """One call per business: keep the best-scoring contact at each company,
    with CONTACTS_AT_COMPANY = how many contacts it had."""
    df = df.sort_values(score_col, ascending=False).copy()
    df["CONTACTS_AT_COMPANY"] = df.groupby("COMPANY_KEY")["COMPANY_KEY"].transform("size")
    return df.drop_duplicates("COMPANY_KEY").copy()


def why(df, class_labels, extras=()):
    """'high-risk premises, owner, small business, ...'. `extras` is a list of
    (column, label) pairs added when that boolean column is True."""
    out = []
    for _, r in df.iterrows():
        h = [class_labels[r["INDUSTRY_CLASS"]], f"{r['BUYER_ROLE']}"]
        if r["SIZE"] in ("small", "enterprise"):
            h.append({"small": "small business", "enterprise": "5,000+ staff"}[r["SIZE"]])
        if "ZIP_CRIME_PCTL" in r and r["ZIP_CRIME_PCTL"] >= 0.75:
            h.append("high property-crime ZIP")
        if r.get("BUILDING"):
            h.append(f"{r['BUILDING']} building")
        h += [label for col, label in extras if bool(r.get(col))]
        out.append(", ".join(h))
    return out
