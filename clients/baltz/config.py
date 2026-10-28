"""Baltz Concrete settings. Every file name is relative to data/baltz/
(raw audience files live in data/raw/)."""
from audiencelab.filtering import MEMPHIS_METRO

AUDIENCE_FILE = "baltz_audience.csv"                 # in data/raw/

# Step outputs (data/baltz/)
RESIDENTIAL_LEADS = "residential_leads.csv"          # step 1: cleaned, not graded
LOOKUP_RESULTS = "shelby_lookup_results.csv"         # step 2: Shelby assessor lookup
ENRICHED_LEADS = "residential_leads_enriched.csv"    # step 3: final graded leads
INVESTOR_LEADS = "residential_investor_leads.csv"    # step 3: company-owned homes
EXCLUDED_LEADS = "residential_excluded.csv"          # step 3: owner differs / tiny lot

# ---- Step 1: filtering ----
SERVICE_AREA = MEMPHIS_METRO
DROP_PO_BOXES = True

# ---- Grading (every Baltz list: residential, commercial, partners) ----
# Rank-based: top 20% = A, next 40% = B, rest = C, within each list.
TIER_SHARES = {"A": 0.20, "B": 0.40}

# ---- Step 3: residential grading ----

MIN_LOT_SQFT = 4_500        # below this: townhome / planned development, HOA flatwork
LARGE_LOT_SQFT = 43_560     # one acre: usually long or gravel drives, a different job

# Starting-point weights, NOT proven. Backtest against past Baltz customers
# and adjust. Max is about 100 for a verified lead.
PROPERTY_POINTS = {
    "verified_base": 30,     # confident parcel match AND passes the ownership checks
    "unverified_base": 15,   # no confident parcel match: neutral, ranks below verified
    "pool": 15,              # pool deck / patio work
    "garage": 5,             # detached garage or carport (no pool): apron / drive work
    "recent_buyer": 15,      # arm's-length purchase within 2 years
    "buyer_2_to_5": 8,       # arm's-length purchase 2-5 years ago
    "long_owner": 10,        # owned 15+ years, arm's-length: aging flatwork
    "built_pre_1995": 10,
    "built_1995_2004": 5,
    "lot_prime": 10,         # 8,000 - 25,000 sq ft: room for flatwork
    "lot_small": 5,          # 4,500 - 8,000 sq ft
    "income_high": 5,        # income >= 75k
    "income_mid": 3,         # income >= 45k
    "worth_high": 5,         # net worth >= 100k
    "worth_mid": 3,          # net worth >= 25k
    "contact_both": 5,       # has both phone and email
}


# ============================================================
# COMMERCIAL LEADS  (clients/baltz/commercial.py)
# ============================================================
# Two lists, each graded A/B/C by rank (TIER_SHARES: top 20% A, next 40% B):
#   commercial_leads.csv          DIRECT: who controls a lot of pavement (parking lots,
#                                 sidewalks, slabs, loading docks). Government is kept
#                                 here, flagged LEAD_TYPE = "public bid", with a slight
#                                 penalty (they buy through public bids, not sales calls).
#   commercial_partner_leads.csv  PARTNERS: GCs, builders, developers (sub out concrete) and
#                                 architects / civil engineers / landscapers (specify it).
# Concrete / paving companies are competitors and are dropped from both.
# Starting-point rules, NOT proven: backtest against Baltz's past commercial jobs.
from audiencelab.commercial import IndustryRules, RoleRules   # noqa: E402

COMMERCIAL_LEADS = "commercial_leads.csv"
COMMERCIAL_PARTNER_LEADS = "commercial_partner_leads.csv"
COMMERCIAL_LOOKUP_INPUT = "commercial_lookup_input.csv"       # company addresses to look up
COMMERCIAL_LOOKUP_RESULTS = "commercial_lookup_results.csv"   # `run.py baltz commercial-lookup`

COMMERCIAL_REQUIRED_COLS = [
    "COMPANY_NAME", "COMPANY_INDUSTRY", "COMPANY_DESCRIPTION", "COMPANY_ADDRESS",
    "COMPANY_CITY", "COMPANY_STATE", "COMPANY_ZIP", "COMPANY_PHONE", "COMPANY_DOMAIN",
    "COMPANY_EMPLOYEE_COUNT", "COMPANY_NAICS", "JOB_TITLE", "SENIORITY_LEVEL",
]

DIRECT_POINTS = {
    # 1. Does the business control a lot of flatwork?
    "industry_high": 35,      # property mgmt, HOAs, apartments, churches, warehouses, dealers...
    "industry_public_bid": 25,  # government: lots of pavement, but bought through public bids
    "industry_medium": 15,    # has a site, less flatwork (manufacturing, clinics, bank branches)
    "industry_office": 0,     # office-only tenant: the landlord controls the parking lot
    "industry_out": -30,      # hospital systems: decided by corporate facilities
    # 2. Can this person buy?
    "role_owner": 30,         # owner, founder, president, CEO, managing partner
    "role_operator": 25,      # facilities, maintenance, property / community manager, operations,
                              # renovations, pastor / bishop, purchasing, public works
    "role_senior": 10,
    "role_staff": 0,
    "buyer_at_flatwork": 10,  # owner or operator AND (high-flatwork OR public bid)
    # 3. Local and small enough to decide on site? (public bids always count as "mid")
    "size_small": 15, "size_mid": 8, "size_large": 0, "size_enterprise": -20,
    # 4. The building (after `run.py baltz commercial-lookup`)
    "owns_building": 15,      # the owner pays for paving
    "leases_building": -5,    # tenant: the landlord (BUILDING_OWNER) is the real buyer
    "big_lot": 10,            # parcel >= 1 acre: big parking lot
    "built_pre_1995": 5,      # older property: aging pavement
    "direct_phone": 5,
}
BIG_LOT_SQFT = 43_560
OLD_BUILDING_YEAR = 1995

PARTNER_POINTS = {
    "type_gc": 35,            # GC, builder, developer: subs out concrete again and again
    "type_design": 20,        # architect, civil engineer, landscaper: specifies / recommends
    "role_owner": 30,         # owner, president, principal
    "role_operator": 25,      # estimator, project manager, preconstruction, purchasing,
                              # superintendent: the people who send bid requests
    "role_senior": 10,
    "role_staff": 0,
    "direct_phone": 5,
}
# Mid-size firms run the most jobs that need a concrete sub.
PARTNER_SIZE_POINTS = {
    "1 to 10": 5, "11 to 25": 10, "26 to 50": 10, "51 to 100": 10, "101 to 250": 10,
    "251 to 500": 10, "501 to 1000": 0, "1001 to 5000": 0, "5001 to 10000": -10, "10000+": -10,
}

COMPETITOR_RE = r"\bconcrete\b|\bpaving\b|\basphalt\b|\bflatwork\b|\bready[- ]mix\b"
PUBLIC_BID_RE = (
    r"\bgovernment\b|\bcity of\b|\bcounty of\b|\bstate of\b|\bshelby county\b|\bdesoto county\b|"
    r"\bcounty commission\b|\bsheriff\b|\bpolice\b|\bfire department\b|\bpublic works\b|"
    r"\bpublic safety\b|\bschool district\b|\bcounty schools\b|\bcity schools\b|\bpublic schools\b|"
    r"\bhousing authority\b|\bairport authority\b|\btransit authority\b|\bport of\b|"
    r"\bfederal (?:government|agency|courthouse|building)\b|\bu\.?s\.? (?:army|navy|air force|postal)\b|"
    r"\bmilitary\b|\butilities\b|\bmlgw\b"
)
OUT_RE = r"(?<!animal )\bhospitals?\b|\bhealth ?(?:care )?system\b|\bmedical center\b|\bhealth science center\b"
PARTNER_GC_RE = (
    r"\bgeneral contractors?\b|\bconstruction\b|\bbuilders?\b|\bhome ?builders?\b|\bcontractors?\b|"
    r"\bdevelopers?\b|\b(?:real estate|land|property|site|commercial|residential) development\b|"
    r"\bremodel\w*"
)
PARTNER_DESIGN_RE = (
    r"\barchitect\w*|\bcivil engineer\w*|\bstructural engineer\w*|\blandscap\w*|"
    r"\bsurvey(?:ing|ors?)\b"
)
UNIVERSITY_RE = r"\buniversity\b|\bcollege\b|\bhigher education\b"
HIGH_RE = (
    r"\bproperty management\b|\bhoa\b|\bhomeowners'? association\b|\bapartments?\b|"
    r"\bmulti-?family\b|\bresidential\b|\breal estate investment\b|\brei\b|\breit\b|\blandlord\b|"
    r"\bleasing\b|\bcommercial real estate\b|\bchurch\w*|\bcathedral\b|\bdiocese\b|\bministr\w*|"
    r"\breligious\b|\bepiscopal\b|\bcatholic\b|\blutheran\b|\bparish\b|\bcogic\b|"
    r"\bacademy\b|\bday school\b|\bprivate school\b|\bwarehous\w*|\blogistics\b|"
    r"\bsupply chain\b|\bdistribution\b|\bfreight\b|\btrucking\b|\btruck\w*|\bstorage\b|"
    r"\bindustrial park\b|\bmaterials handling\b|\bhotels?\b|\bmotels?\b|\bhospitality\b|"
    r"\bauto\b|\bautomotive\b|\bdealer\w*|\bcar wash\b|\bgas station|\bconvenience\b|"
    r"\brestaurants?\b|\bretail\w*|\bshopping center\b|\bplaza\b|\bgolf\b|\bcountry club\b|"
    r"\bparking\b|\bsenior living\b|\bassisted living\b|\bnursing home\b|\bretirement\b|"
    r"\bdaycare\b|\bchild ?care\b|\bfuneral\b|\bcemeter\w*|\bfacilities services\b"
)
MEDIUM_RE = (
    r"\bmanufactur\w*|\bindustrial\b|\bmachinery\b|\bwholesale\b|\bfurniture\b|\bfood\b|"
    r"\bbeverage\b|\bagricultur\w*|\bfarming\b|\bhealth\b|\bwellness\b|\bmedical\b|\bfitness\b|"
    r"\bclinics?\b|\bdental\b|\bbanks?\b|\bbanking\b|\bcredit union\b|\bnon-?profit\b|"
    r"\bfundraising\b|\bmuseums?\b|\bevents?\b|\bpaper\b|\bprinting\b|\bplastics\b|"
    r"\bchemicals?\b|\beducation\b|\bschools?\b|\breal estate\b|\btransportation\b"
)
OFFICE_RE = (
    r"\blaw\b|\blegal\b|\baccount\w*|\bconsult\w*|\bit services\b|\binformation technology\b|"
    r"\bsoftware\b|\bcomputer\b|\binternet\b|\bmarketing\b|\badvertis\w*|\bfinancial\b|"
    r"\binvestment\b|\bventure\b|\bcapital\b|\binsurance\b|\bstaffing\b|\brecruit\w*|"
    r"\bresearch\b|\bdesign\b|\bmedia\b|\btelecom\w*|\bpublishing\b|\bsecurities\b|"
    r"\bcoaching\b|\btraining\b|\bengineering\b"
)
# A "real estate" firm (classed medium) whose description says it manages property
# is a direct high-flatwork lead.
PROPERTY_MANAGER_DESC_RE = (
    r"\bproperty management\b|\bapartment communit\w*|\bmulti-?family\b|"
    r"\bmanag\w*[\w ,]{0,40}\bpropert\w*|\bcommercial real estate\b|\bself[- ]storage\b|"
    r"\bhomeowners'? association"
)

COMMERCIAL_INDUSTRY = IndustryRules(
    # The name decides only for strong words; vague names fall through to the industry field.
    classes=[("competitor", "competitor", COMPETITOR_RE), ("public_bid", "public_bid", PUBLIC_BID_RE),
             ("out", "out", OUT_RE), ("partner_gc", "partner_gc", PARTNER_GC_RE),
             ("partner_design", "partner_design", PARTNER_DESIGN_RE),
             ("university", "medium", UNIVERSITY_RE), ("high", "high", HIGH_RE),
             ("medium", "medium", MEDIUM_RE), ("office", "office", OFFICE_RE)],
    name_labels={"competitor", "public_bid", "out", "partner_gc", "partner_design", "university", "high"},
    naics={
        "92": "public_bid", "622": "out", "23811": "competitor", "23": "partner_gc",
        "54131": "partner_design", "54132": "partner_design", "56173": "partner_design",
        "5311": "high", "5313": "high", "53113": "high", "493": "high", "484": "high", "488": "high",
        "492": "high", "44": "high", "45": "high", "722": "high", "7211": "high", "8131": "high",
        "6244": "high", "623": "high", "8122": "high", "71391": "high",
        "31": "medium", "32": "medium", "33": "medium", "42": "medium", "62": "medium",
        "61": "medium", "5221": "medium", "81": "medium", "11": "medium",
        "51": "office", "52": "office", "53": "office", "54": "office", "55": "office",
    },
    name_skip_re=r"\bsociety\b|(?<!homeowners )(?<!homeowners' )\bassociation\b|\binstitute\b|\bcouncil\b|\bfederation\b|\bfraternity\b",
    description_upgrade=(("medium",), "high", PROPERTY_MANAGER_DESC_RE),   # real-estate firms only
)

_OWNER_RE = (r"\bowner\b|\bco-?owner\b|\bfounder\b|\bco-?founder\b|\bproprietor\b|\bfranchisee\b|"
             r"\bpresident\b|\bceo\b|\bchief executive\b|\bmanaging partner\b")
DIRECT_ROLES = RoleRules(
    roles=[("owner", _OWNER_RE),
           ("operator",
            r"\bgeneral manager\b|\bgm\b|\boperations\b|\bcoo\b|\bfacilit\w*|\bproperty manager\b|"
            r"\bcommunity manager\b|\basset manager\b|(?<!website )(?<!software )\bmaintenance\b|\bplant manager\b|"
            r"\bwarehouse manager\b|\bsite manager\b|\bstore manager\b|\bbranch manager\b|"
            r"\boffice manager\b|\bbusiness manager\b|\badministrator\b|\bexecutive director\b|"
            r"\bpastor\b|\brector\b|\bbishop\b|\bpriest\b|\bhead of school\b|\bsuperintendent\b|"
            r"\bpublic works\b|\bpurchasing\b|\bprocurement\b|\bconstruction manager\b|"
            r"\brenovations?\b|\bcapital projects\b|\breal estate manager\b")],
    senior_first_blocks={"owner"},   # "Vice President" is never the owner, but a
                                     # "VP - Director of Renovations" is still the buyer
)
PARTNER_ROLES = RoleRules(
    roles=[("owner", _OWNER_RE + r"|\bprincipal\b"),
           ("operator",
            r"\bestimat\w*|\bproject manager\b|\bproject executive\b|\bpre-?construction\b|"
            r"\bpurchasing\b|\bprocurement\b|\bsuperintendent\b|\boperations\b|"
            r"\bconstruction manager\b|\bconstruction\b|\bdevelopment manager\b|\bcorporate development\b")],
    senior_first_blocks={"owner"},
)
FRANCHISE_OWNER_RE = r"\bowner\b|\bco-?owner\b|\bfranchisee\b|\bproprietor\b"
