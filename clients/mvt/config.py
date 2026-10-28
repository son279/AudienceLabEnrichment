"""MVT (security cameras) settings. Every file name is relative to data/mvt/
(raw audience files live in data/raw/)."""
from audiencelab.commercial import IndustryRules, RoleRules
from audiencelab.filtering import MEMPHIS_METRO

AUDIENCE_FILE = "mvt_audience.csv"                    # in data/raw/

# Step outputs (data/mvt/)
RESIDENTIAL_LEADS = "securitycam_residential_leads.csv"  # step 1: cleaned + pre-scored
LOOKUP_RESULTS = "shelby_lookup_results.csv"             # step 2: Shelby assessor lookup
CRIME_FILE = "mpd_crime.csv"                             # from `run.py mvt records crime`
CONSUMER_LEADS = "securitycam_consumer_leads.csv"        # step 3: final graded leads
LANDLORD_LEADS = "securitycam_landlord_leads.csv"        # step 3: company-owned homes
EXCLUDED_LEADS = "securitycam_excluded.csv"              # step 3: likely renters

# ---- Grading (every MVT list: residential pre-score, consumer leads, commercial) ----
# Rank-based: top 20% = A, next 40% = B, rest = C, within each list.
TIER_SHARES = {"A": 0.20, "B": 0.40}

# ---- Step 1: filtering + pre-score ----
SERVICE_AREA = MEMPHIS_METRO     # PLACEHOLDER: replace with MVT's real service map
DROP_PO_BOXES = False

# ---- Step 3: grading ----
# MVT matches leads to parcels with a broader unit pattern than step 1
# (also strips Ste/Suite/Bldg), so "123 Main St Ste 4" keys as "123 main st".
ENRICH_UNIT_RE = r"\b(?:apt|unit|ste|suite|#|bldg|lot)\s*[\w-]+\b"

LARGE_LOT_SQFT = 43_560      # 1+ acre: long driveway / perimeter = more cameras
VALUE_HIGH, VALUE_MID = 300_000, 150_000

# Starting-point weights, NOT proven. Backtest against past MVT installs and
# adjust. Max is about 100 for a verified lead.
PROPERTY_POINTS = {
    "verified_base": 20,     # confident parcel match AND passes the ownership checks
    "unverified_base": 10,   # no confident parcel match: neutral, ranks below verified
    "moved_in_1yr": 20,      # arm's-length purchase within 1 year: #1 camera trigger
    "moved_in_2yr": 12,      # arm's-length purchase 1-2 years ago
    "new_construction": 10,  # built in the last 2 years: builder rarely installs cameras
    "crime_high": 15,        # ZIP in the top 25% for property crime
    "crime_mid": 8,          # ZIP in the 50-75th percentile
    "value_high": 8,         # appraisal or last sale >= $300k: more to protect
    "value_mid": 4,          # >= $150k
    "large_lot": 5,          # 1+ acre: driveway / perimeter coverage
    "outbuilding": 5,        # detached garage, carport, shed, barn, workshop
    "pool": 3,               # pool area / gate monitoring
    "income_high": 5,        # income >= 75k
    "income_mid": 3,         # income >= 45k
    "worth_high": 5,         # net worth >= 100k
    "worth_mid": 3,          # net worth >= 25k
    "contact_both": 5,       # has both phone and email
}

# Crime file: column names vary by export, so the first match is used.
CRIME_ZIP_COLS = ["ZIP", "ZIP_CODE", "Zip Code", "zip", "zipcode", "Zip"]
CRIME_TYPE_COLS = ["Category", "CATEGORY", "Offense", "OFFENSE", "Offense Description",
                   "OFFENSE_DESCRIPTION", "Crime Type", "CRIME_TYPE", "UCR Category",
                   "agency_crimetype_id"]
PROPERTY_CRIME_RE = (r"BURGL|THEFT|LARCEN|VANDAL|ROBBER|MOTOR VEHICLE|STOLEN|BREAK|"
                     r"TRESPASS|DAMAGE|PROWL|SHOPLIFT")


# ============================================================
# COMMERCIAL LEADS  (clients/mvt/commercial.py)
# ============================================================
# A good commercial lead = a decision maker at a LOCAL business with PREMISES
# worth watching. Starting-point rules, NOT proven: backtest against MVT's past
# commercial customers and adjust.

COMMERCIAL_LEADS = "securitycam_commercial_leads.csv"
COMMERCIAL_LOOKUP_INPUT = "commercial_lookup_input.csv"       # company addresses to look up
COMMERCIAL_LOOKUP_RESULTS = "commercial_lookup_results.csv"   # `run.py mvt commercial-lookup`

COMMERCIAL_REQUIRED_COLS = [
    "COMPANY_NAME", "COMPANY_INDUSTRY", "COMPANY_DESCRIPTION", "COMPANY_ADDRESS",
    "COMPANY_CITY", "COMPANY_STATE", "COMPANY_ZIP", "COMPANY_PHONE", "COMPANY_DOMAIN",
    "COMPANY_EMPLOYEE_COUNT", "COMPANY_NAICS", "JOB_TITLE", "SENIORITY_LEVEL",
]

COMMERCIAL_POINTS = {
    # 1. Does the business have a site that needs cameras?
    "industry_high": 35,      # inventory, cash, after-hours or foot traffic
    "industry_medium": 15,    # has a building, lower risk (manufacturing, wholesale...)
    "industry_office": 0,     # office-only: law, accounting, IT, finance, marketing
    "industry_out": -30,      # buys through corporate / specialist vendors
    # 2. Can this person buy?
    "role_owner": 30,         # owner, founder, president, CEO, managing partner
    "role_operator": 25,      # GM, operations, facilities, property / store / branch manager
    "role_senior": 10,        # other director / VP / manager / partner
    "role_staff": 0,
    "buyer_at_high_risk": 10, # owner or operator AND a high-risk industry
    # 3. Local and small enough to decide on site?
    "size_small": 15,         # 1-50 employees (or the contact is the owner/franchisee)
    "size_mid": 8,            # 51-500
    "size_large": 0,          # 501-5,000
    "size_enterprise": -20,   # 5,001+: decided at corporate
    # Extras
    "crime_high": 10,         # company ZIP in the top 25% for property crime
    "crime_mid": 5,           # 50-75th percentile
    "owns_building": 10,      # assessor shows the company / contact owns the parcel
    "direct_phone": 5,        # a callable phone for the contact
}
# Graded with TIER_SHARES (top of this file), like every other list.

EMPLOYEE_SIZE = {
    "1 to 10": "small", "11 to 25": "small", "26 to 50": "small",
    "51 to 100": "mid", "101 to 250": "mid", "251 to 500": "mid",
    "501 to 1000": "large", "1001 to 5000": "large",
    "5001 to 10000": "enterprise", "10000+": "enterprise",
}

# Industry is read from the company NAME, then COMPANY_INDUSTRY, then NAICS, then
# the description (never the job title, so a "Director of Pharmacy" at a hospital
# doesn't count as a pharmacy). At each level the first class that matches wins,
# in this order: out, university (medium), high, medium, office. The name only
# decides when it says out / university / high ("Corky's BBQ", "City of Memphis");
# vaguer names ("... Health Care") fall through to the industry field.
# Names of associations / societies never decide (a dentists' society isn't a clinic).
NAME_SKIP_RE = r"\bsociety\b|\bassociation\b|\binstitute\b|\bcouncil\b|\bfederation\b"
# Owners of a big brand are usually franchisees, so these titles count as a small business.
FRANCHISE_OWNER_RE = r"\bowner\b|\bco-?owner\b|\bfranchisee\b|\bproprietor\b"
INDUSTRY_OUT_RE = (
    r"(?<!animal )\bhospitals?\b|\bhealth ?(?:care )?system\b|\bmedical center\b|"
    r"\bgovernment\b|\bcity of\b|\bcounty of\b|\bstate of\b|\bfederal\b|\bmilitary\b|"
    r"\bbanking\b|\bbank\b|\bcredit union\b"
)
INDUSTRY_UNIVERSITY_RE = r"\buniversity\b|\bcollege\b|\bhigher education\b"
INDUSTRY_HIGH_RE = (
    r"\bretail\w*|\bstores?\b|\bshops?\b|\bdealer\w*|\bautomotive\b|\bauto\b|\bcar wash\b|"
    r"\bgas station|\bconvenience\b|\bwarehous\w*|\blogistics\b|\bsupply chain\b|"
    r"\bdistribution\b|\bfreight\b|\btrucking\b|\btruck center\b|\b(?:self[- ])?storage\b|"
    r"\brestaurants?\b|\bbars?\b|\bbbq\b|\bgrill\b|\bcafe\b|\bpharmac\w*|\bjewel\w*|"
    r"\bcannabis\b|\bdispensar\w*|\bapartments?\b|\bmultifamily\b|\bproperty management\b|"
    r"\bleasing\b|\bchurch\w*|\bcathedral\b|\bministr\w*|\breligious\b|\bschools?\b|"
    r"\bdaycare\b|\bchild ?care\b|\bpreschool\b|\bacademy\b|\bclinics?\b|\bdental\b|"
    r"\bdentist\w*|\bveterinar\w*|\banimal hospital\b|\bconstruction\b|\bcontractors?\b|"
    r"\bhotels?\b|\bmotels?\b|\bhospitality\b|\bsalons?\b|\bgym\b|\bfitness\b|\bparking\b|"
    r"\bloading\b|\bmoving\b|\bhauling\b|\bscaffold\w*|\btowing\b|\bpawn\b"
)
INDUSTRY_MEDIUM_RE = (
    r"\bmanufactur\w*|\bindustrial\b|\bmachinery\b|\bwholesale\b|\bfurniture\b|"
    r"\bfood\b|\bagricultur\w*|\bfarming\b|\bhealth\b|\bwellness\b|\bmedical\b|"
    r"\bconsumer services\b|\bevents?\b|\bnon-?profit\b|\bfundraising\b|\breal estate\b|"
    r"\btransportation\b|\bpaper\b|\bprinting\b|\bplastics\b|\bchemicals?\b"
)
INDUSTRY_OFFICE_RE = (
    r"\blaw\b|\blegal\b|\baccount\w*|\bconsult\w*|\bit services\b|\binformation technology\b|"
    r"\bsoftware\b|\bcomputer\b|\binternet\b|\bmarketing\b|\badvertis\w*|\bfinancial\b|"
    r"\binvestment\b|\bventure\b|\bcapital\b|\binsurance\b|\bstaffing\b|\brecruit\w*|"
    r"\barchitecture\b|\bengineering\b|\benvironmental services\b|\bresearch\b|\bdesign\b|"
    r"\bmedia\b|\btelecom\w*|\bsecurities\b"
)
# NAICS prefix -> class (longest prefix wins). Used when name + industry don't match.
NAICS_CLASS = {
    "92": "out", "622": "out", "5221": "out",
    "44": "high", "45": "high", "493": "high", "484": "high", "488": "high", "492": "high",
    "722": "high", "8111": "high", "5311": "high", "5313": "high", "53113": "high",
    "8131": "high", "6111": "high", "6244": "high", "6211": "high", "6212": "high",
    "6213": "high", "54194": "high", "23": "high", "7211": "high", "71394": "high",
    "8121": "high", "81293": "high",
    "31": "medium", "32": "medium", "33": "medium", "42": "medium", "62": "medium",
    "61": "medium", "71": "medium", "72": "medium", "56": "medium", "81": "medium",
    "11": "medium", "48": "medium", "49": "medium", "6113": "medium",
    "51": "office", "52": "office", "53": "office", "54": "office", "55": "office",
}

# Job titles (lower-cased). "vice president" is checked first so it never counts as owner.
ROLE_SENIOR_FIRST_RE = r"\bvice[- ]president\b|\bvp\b|\bassistant\b|\bdeputy\b"
ROLE_OWNER_RE = (r"\bowner\b|\bco-?owner\b|\bfounder\b|\bco-?founder\b|\bproprietor\b|"
                 r"\bfranchisee\b|\bpresident\b|\bceo\b|\bchief executive\b|\bmanaging partner\b")
ROLE_OPERATOR_RE = (
    r"\bgeneral manager\b|\bgm\b|\boperations\b|\bcoo\b|\bfacilit\w*|\bproperty manager\b|"
    r"\bstore manager\b|\bbranch manager\b|\boffice manager\b|\bplant manager\b|"
    r"\bwarehouse manager\b|\bsite manager\b|\bsecurity\b|\bloss prevention\b|"
    r"\bmaintenance\b|\badministrator\b|\bexecutive director\b|\bpastor\b|\brector\b|"
    r"\bhead of school\b"
)
ROLE_SENIORITY = {"cxo", "vp", "director", "manager", "owner", "partner"}

COMMERCIAL_INDUSTRY = IndustryRules(
    classes=[("out", "out", INDUSTRY_OUT_RE), ("university", "medium", INDUSTRY_UNIVERSITY_RE),
             ("high", "high", INDUSTRY_HIGH_RE), ("medium", "medium", INDUSTRY_MEDIUM_RE),
             ("office", "office", INDUSTRY_OFFICE_RE)],
    name_labels={"out", "university", "high"},
    naics=NAICS_CLASS,
    name_skip_re=NAME_SKIP_RE,
    small_hospital_class="high",
)
COMMERCIAL_ROLES = RoleRules(
    roles=[("owner", ROLE_OWNER_RE), ("operator", ROLE_OPERATOR_RE)],
    senior_first_re=ROLE_SENIOR_FIRST_RE,
    seniority=ROLE_SENIORITY,
)

SHELBY_ZIP_PREFIXES = ("380", "381")   # the assessor lookup only covers Shelby County
