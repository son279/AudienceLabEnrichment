"""
clients/mvt/public_records.py
-----------------------------
Pulls public records for the MVT security camera lead pipeline with Playwright.
Two browser agents crawl the two counties at the same time, while the crime
numbers are pulled alongside them:

  DeSoto agent   DeSoto County, MS land records (AcclaimWeb): sale deeds recorded in
                 the last N days, joined to the DeSoto GIS tax parcels for the address,
                 lot size and appraised value.        -> desoto_recent_deeds.csv
  Shelby agent   Shelby County, TN Register of Deeds: warranty deeds recorded in the
                 last N days, with property address and sale price straight from
                 the index.                           -> shelby_recent_deeds.csv
  crime          MPD Public Safety Incidents (Memphis Data Hub): property-crime counts
                 per ZIP, summed on the server.       -> mpd_crime.csv
                 (the optional crime input of `run.py mvt enrich`)

  Both counties are also merged into recent_deeds_all.csv (same columns + COUNTY).

Setup (once):
    pip install playwright pandas
    playwright install chromium

Run (files land in data/mvt/):
    python run.py mvt records all --days 30          # both agents + crime
    python run.py mvt records deeds --days 14        # both agents only
    python run.py mvt records desoto | shelby | crime

Be a good citizen: these are free county / city services. Each agent waits
REQUEST_PAUSE_SEC between its own requests and searches one day (Shelby) or one
week (DeSoto) at a time. Don't lower the pause or start more agents.
"""
import asyncio
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
from playwright.async_api import async_playwright

# ============================================================
# CONFIG
# ============================================================

REQUEST_PAUSE_SEC = 1.5
OUT_DIR = Path(".")             # set by main(); run.py passes data/mvt/
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

# --- MPD crime (Memphis Data Hub, item 12b51ce4d5a14493ab6cc05d32e0c1ee) ---
MPD_URL = ("https://services2.arcgis.com/saWmpKJIUAjyyNVc/arcgis/rest/services/"
           "MPD_Public_Safety_Incidents/FeatureServer/0/query")
CRIME_COLUMNS = {                      # UCR category -> output column
    "BURG/BREAK & ENTER": "BURGLARY",
    "LARCENY/THEFT": "THEFT",
    "MOTOR VEHICLE THEFT": "VEHICLE_THEFT",
    "DEST/DAM/VAND OF PROPERTY": "VANDALISM",
    "ROBBERY": "ROBBERY",
    "STOLEN PROPERTY": "STOLEN_PROPERTY",
    "ARSON": "ARSON",
}
MEMPHIS_ZIP_RE = r"^38\d{3}$"

# --- DeSoto County land records (AcclaimWeb) ---
ACCLAIM_BASE = "https://landrecords.desotocountyms.gov/AcclaimWeb"
ACCLAIM_SEARCH = ACCLAIM_BASE + "/search/SearchTypeDocType"
ACCLAIM_GRID = ACCLAIM_BASE + "/Search/GridResults"
# Sale deeds only: quitclaims are mostly family transfers, trustee deeds are foreclosures.
DESOTO_DEED_TYPES = {
    "1509": "WAR - WARRANTY",
    "1424": "SWT - SPECIAL WARRANTY",
    "1045": "COR - CORPORATE WARRANTY DEED",
    "1080": "DEE - DEED",
    "1090": "DOS - DEED OF SALE",
}
DESOTO_CHUNK_DAYS = 7
DESOTO_PAGE_SIZE = 1000

DESOTO_PARCELS_URL = ("https://gis.desotocountyms.gov/arcgis/rest/services/"
                      "BaseLayers/MapServer/1/query")
PARCEL_FIELDS = ["PIN", "FULL_ADDR", "CITY", "ZIP_CODE", "OWNER_NAME", "OWNER_ADDRESS",
                 "ACREAGE", "TOT_APVAL", "LANDVALUE", "IMPCLS1", "EXEMPT", "SUBD_NAME",
                 "LOT", "DEED_BOOK1", "DEED_PAGE1", "Y", "X"]
PIN_BATCH = 50

# --- Shelby County Register of Deeds ---
SHELBY_HOME = "https://search.register.shelby.tn.us/search/index.php"
SHELBY_SEARCH = "https://search.register.shelby.tn.us/search/content.php?state=tn&county=shelby"
# Checkbox values ticked on the search form (the WARRANTYDEED group + WD doc type).
SHELBY_DOC_TYPES = {"WARRANTYDEED", "WD"}
SHELBY_MAX_ROWS = 1000          # the site stops at 1000 rows per search

# --- Owner type rules (same as the enrichment scripts, plus builder words) ---
LEGAL_ENTITY_RE = r"\b(?:LLC|L L C|INC|LP|LLP|LTD|CORP|CO|COMPANY|GP)\b"
ENTITY_WORD_RE = (r"\b(?:HOLDINGS|PROPCO|CAPITAL|INVEST\w*|PROPERT(?:Y|IES)|MANAGEMENT|SFR|"
                  r"HOMES|BUILDERS?|CONSTRUCTION|DEVELOPMENT|ENTERPRISES|HOUSING|BANK|"
                  r"MORTGAGE|SECRETARY|CHURCH|MINISTRIES|CITY OF|COUNTY|STATE OF|RENTALS?)\b")
TRUST_RE = r"\b(?:TRUST|TR|TRUSTEE)\b"
BUILDER_RE = r"\b(?:HOMES|BUILDERS?|CONSTRUCTION|DEVELOPMENT|DEVELOPERS?|COMMUNITIES)\b"
NAME_SUFFIXES = {"JR", "SR", "II", "III", "IV"}


def owner_type(name):
    if not isinstance(name, str) or not name.strip() or name.strip().upper() == "NONE":
        return "unknown"
    o = name.upper()
    if re.search(LEGAL_ENTITY_RE, o):
        return "entity"
    if re.search(TRUST_RE, o):
        return "trust"
    if re.search(ENTITY_WORD_RE, o):
        return "entity"
    return "person"


def first_person(names):
    """First buyer that is a person, so 'SMITH FAMILY TRUST | SMITH JOHN TR' still yields John Smith."""
    for n in names:
        if owner_type(n) == "person":
            return n
    for n in names:   # trustees: 'KATZ BENJAMIN L TR' -> person name without the TR
        stripped = re.sub(r"\s+(?:TR|TRUSTEE)$", "", n.strip(), flags=re.I)
        if stripped != n.strip() and owner_type(stripped) == "person":
            return stripped
    return ""


def log(agent, msg):
    print(f"[{agent:>6}] {msg}", flush=True)


async def pause():
    await asyncio.sleep(REQUEST_PAUSE_SEC)


def date_chunks(start, end, days):
    d = start
    while d <= end:
        e = min(d + timedelta(days=days - 1), end)
        yield d, e
        d = e + timedelta(days=1)


def look_back(days):
    end = date.today() - timedelta(days=1)       # both indexes run a day or more behind
    return end - timedelta(days=days - 1), end


# Column layout shared by both county files (and the merged file).
LEAD_COLS = ["COUNTY", "DEED_DATE", "DAYS_SINCE_DEED", "DEED_TYPE", "INSTRUMENT",
             "FIRST_NAME", "LAST_NAME", "BUYER_NAMES", "BUYER_TYPE", "SELLER_NAMES",
             "SELLER_TYPE", "HOMEOWNER_LEAD", "NEW_CONSTRUCTION", "PROPERTY_ADDRESS", "CITY",
             "STATE", "ZIP", "SALE_PRICE", "APPRAISED_VALUE", "BUILDING_VALUE", "LOT_ACRES",
             "SUBDIVISION", "LOT", "PIN", "LATITUDE", "LONGITUDE"]


# ============================================================
# CRIME  (runs on Playwright's HTTP client, no browser needed)
# ============================================================


async def pull_crime(api, months):
    since = (date.today() - timedelta(days=round(months * 30.44))).isoformat()
    cats = ", ".join(f"'{c}'" for c in CRIME_COLUMNS)
    stats = '[{"statisticType":"count","onStatisticField":"ObjectId","outStatisticFieldName":"N"}]'
    rows, offset = [], 0
    while True:
        resp = await api.post(MPD_URL, form={
            "f": "json",
            "where": f"Offense_Datetime >= DATE '{since}' AND UCR_Category IN ({cats})",
            "groupByFieldsForStatistics": "ZIP_Code,UCR_Category",
            "outStatistics": stats,
            "orderByFields": "ZIP_Code,UCR_Category",
            "resultOffset": str(offset),
        })
        data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"MPD query failed: {data['error']}")
        feats = [f["attributes"] for f in data.get("features", [])]
        rows += feats
        if not data.get("exceededTransferLimit") or not feats:
            break
        offset += len(feats)
        await pause()

    df = pd.DataFrame(rows, columns=["ZIP_Code", "UCR_Category", "N"])
    df = df[df["ZIP_Code"].astype("string").str.match(MEMPHIS_ZIP_RE, na=False)]
    wide = (df.assign(COL=df["UCR_Category"].str.strip().map(CRIME_COLUMNS))
              .pivot_table(index="ZIP_Code", columns="COL", values="N", aggfunc="sum", fill_value=0))
    for col in CRIME_COLUMNS.values():
        if col not in wide.columns:
            wide[col] = 0
    wide = wide[list(CRIME_COLUMNS.values())]
    wide.insert(0, "PROPERTY_CRIME_COUNT", wide.sum(axis=1))
    wide = wide.reset_index().rename(columns={"ZIP_Code": "ZIP"})
    wide["SINCE"] = since
    wide = wide.sort_values("PROPERTY_CRIME_COUNT", ascending=False)
    wide.to_csv(OUT_DIR / "mpd_crime.csv", index=False)
    log("crime", f"{len(wide)} Memphis ZIPs, {int(wide['PROPERTY_CRIME_COUNT'].sum()):,} "
                 f"property crimes since {since} -> mpd_crime.csv")
    return wide


# ============================================================
# DESOTO AGENT
# ============================================================

# Runs inside the AcclaimWeb page so it uses that browser's session cookie:
# posts the Record Date search form, then reads every page of the results grid.
ACCLAIM_JS = """
async ({types, from, to, size, grid}) => {
  const f = document.forms[0];
  const fd = new URLSearchParams(new FormData(f));
  fd.set('DocTypes', types);
  fd.set('RecordDateFrom', from);
  fd.set('RecordDateTo', to);
  const s = await fetch(f.action, {method: 'POST', body: fd,
      headers: {'Content-Type': 'application/x-www-form-urlencoded'}});
  if (!s.ok) throw new Error('search failed: ' + s.status);
  let page = 1, out = [], total = null;
  while (true) {
    const r = await fetch(grid, {method: 'POST',
        headers: {'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                  'X-Requested-With': 'XMLHttpRequest'},
        body: `page=${page}&size=${size}&orderBy=~&groupBy=~&filter=~`});
    const j = await r.json();
    total = j.total;
    out = out.concat(j.data || []);
    if (!j.data || !j.data.length || out.length >= total) break;
    page += 1;
    await new Promise(res => setTimeout(res, 1500));
  }
  return {total, rows: out};
}
"""


async def accept_disclaimer(page):
    for label in ("I Accept", "Accept", "I Agree", "Agree"):
        btn = page.get_by_role("button", name=label)
        if await btn.count():
            await btn.first.click()
            await page.wait_for_load_state("networkidle")
            return


def acclaim_date(value):
    m = re.search(r"\d+", str(value))
    return datetime.fromtimestamp(int(m.group()) / 1000, tz=timezone.utc).date() if m else None


def split_names(value, sep=r"<br\s*/?>"):
    """Split an index cell into names and drop ET AL / ET UX noise."""
    parts = re.split(sep, str(value or ""), flags=re.I)
    parts = [re.sub(r"\bET\s*AL\b|\bET\s*UX\b|\bETVIR\b", "", re.sub(r"<[^>]+>", "", p),
                    flags=re.I).strip(" ,") for p in parts]
    return [p for p in dict.fromkeys(parts) if p and p.upper() != "NONE"]


def desoto_first_last(name):
    """DeSoto index is 'LAST,FIRST MIDDLE'."""
    if owner_type(name) != "person" or "," not in name:
        return "", ""
    last, first = name.split(",", 1)
    return first.strip().title(), last.strip().title()


PARCEL_RE = re.compile(r"Parcel\s*#\s*(\d{6,})\s+(\d{4,})", re.I)


def pins_from_legal(legal):
    """'..., Parcel # 109736120      0011600' -> ['109736120      0011600'] (GIS PIN format)."""
    return list(dict.fromkeys(f"{a.ljust(15)}{b}" for a, b in PARCEL_RE.findall(str(legal or ""))))


def desoto_deeds_frame(raw):
    out = []
    for r in raw:
        buyers, sellers = split_names(r.get("IndirectName")), split_names(r.get("DirectName"))
        pins = pins_from_legal(r.get("DocLegalDescription")) or [None]
        first, last = desoto_first_last(first_person(buyers))
        for pin in pins:
            out.append({
                "PIN": pin,
                "DEED_DATE": acclaim_date(r.get("RecordDate")),
                "DEED_TYPE": r.get("DocType"),
                "INSTRUMENT": re.sub(r"\s+", "", str(r.get("BookPage") or "")),
                "BUYER_NAMES": " | ".join(buyers),
                "FIRST_NAME": first,
                "LAST_NAME": last,
                "BUYER_TYPE": owner_type(buyers[0]) if buyers else "unknown",
                "SELLER_NAMES": " | ".join(sellers),
                "SELLER_TYPE": owner_type(sellers[0]) if sellers else "unknown",
                "LEGAL_DESCRIPTION": r.get("DocLegalDescription"),
            })
    return pd.DataFrame(out)


async def desoto_parcels(api, pins):
    rows = []
    pins = [p for p in dict.fromkeys(pins) if p]
    for i in range(0, len(pins), PIN_BATCH):
        batch = pins[i:i + PIN_BATCH]
        resp = await api.post(DESOTO_PARCELS_URL, form={
            "f": "json", "returnGeometry": "false", "outFields": ",".join(PARCEL_FIELDS),
            "where": "PIN IN (" + ",".join(f"'{p}'" for p in batch) + ")",
        })
        data = await resp.json()
        if "error" in data:
            raise RuntimeError(f"DeSoto GIS query failed: {data['error']}")
        rows += [f["attributes"] for f in data.get("features", [])]
        await pause()
    return pd.DataFrame(rows, columns=PARCEL_FIELDS).drop_duplicates("PIN")


def desoto_leads(deeds, parcels):
    df = deeds.merge(parcels, on="PIN", how="left")
    imp = pd.to_numeric(df["IMPCLS1"], errors="coerce")
    roll_owner = df["OWNER_NAME"].fillna("").str.upper()
    # The tax roll still shows the seller until it updates, so a company selling a
    # parcel with no building value (or one on the preliminary roll) = a brand-new house.
    df["NEW_CONSTRUCTION"] = (df["SELLER_TYPE"].eq("entity")
                              & (imp.fillna(0).eq(0) | roll_owner.str.contains("PRELIMINARY")))
    df = df.rename(columns={
        "FULL_ADDR": "PROPERTY_ADDRESS", "ZIP_CODE": "ZIP", "ACREAGE": "LOT_ACRES",
        "TOT_APVAL": "APPRAISED_VALUE", "IMPCLS1": "BUILDING_VALUE",
        "SUBD_NAME": "SUBDIVISION", "Y": "LATITUDE", "X": "LONGITUDE"})
    df["COUNTY"], df["STATE"] = "DESOTO", "MS"
    # Latest deed per property.
    df = df.sort_values("DEED_DATE", ascending=False)
    return pd.concat([df[df["PIN"].notna()].drop_duplicates("PIN"), df[df["PIN"].isna()]])


async def desoto_agent(browser, api, days):
    agent = "desoto"
    ctx = await browser.new_context(user_agent=USER_AGENT)
    page = await ctx.new_page()
    try:
        await page.goto(ACCLAIM_SEARCH, wait_until="networkidle")
        await accept_disclaimer(page)
        if "SearchTypeDocType" not in page.url:
            await page.goto(ACCLAIM_SEARCH, wait_until="networkidle")
        start, end = look_back(days)
        raw = []
        for a, b in date_chunks(start, end, DESOTO_CHUNK_DAYS):
            fmt = lambda d: f"{d.month}/{d.day}/{d.year}"
            res = await page.evaluate(ACCLAIM_JS, {
                "types": ",".join(DESOTO_DEED_TYPES), "from": fmt(a), "to": fmt(b),
                "size": DESOTO_PAGE_SIZE, "grid": ACCLAIM_GRID})
            log(agent, f"{a} to {b}: {res['total']} deeds")
            raw += res["rows"]
            await pause()
    finally:
        await ctx.close()

    deeds = desoto_deeds_frame(raw)
    if deeds.empty:
        log(agent, "nothing recorded in that window")
        return pd.DataFrame(columns=LEAD_COLS)
    parcels = await desoto_parcels(api, deeds["PIN"].dropna().tolist())
    leads = finish_leads(desoto_leads(deeds, parcels))
    out = leads.rename(columns={"LANDVALUE": "LAND_VALUE"})
    out = out.rename(columns={"OWNER_NAME": "ROLL_OWNER_NAME", "OWNER_ADDRESS": "ROLL_OWNER_MAILING"})
    keep = LEAD_COLS + ["EXEMPT", "LAND_VALUE", "ROLL_OWNER_NAME", "ROLL_OWNER_MAILING",
                        "LEGAL_DESCRIPTION"]
    out[[c for c in keep if c in out.columns]].to_csv(OUT_DIR / "desoto_recent_deeds.csv", index=False)
    log(agent, f"{len(raw)} deeds -> {len(leads)} properties "
               f"({int(leads['PROPERTY_ADDRESS'].notna().sum())} with an address, "
               f"{int(leads['HOMEOWNER_LEAD'].sum())} homeowner leads) -> desoto_recent_deeds.csv")
    return leads


# ============================================================
# SHELBY AGENT
# ============================================================

# Runs inside the Shelby search page: ticks only the warranty-deed boxes, posts the
# form for one date range, and returns the raw cells of the results table.
SHELBY_JS = """
async ({types, from, to}) => {
  const f = document.getElementById('davidson_form');
  if (!f) throw new Error('Shelby search form not found');
  f.querySelectorAll('input[type=checkbox]').forEach(c => c.checked = types.includes(c.value));
  f.querySelectorAll('input[type=text]').forEach(i => i.value = '');
  f.start_date.value = from;
  f.end_date.value = to;
  const url = 'content.php?random=' + Math.floor(Math.random() * 1e7);
  const r = await fetch(url, {method: 'POST', body: new FormData(f)});
  if (!r.ok) throw new Error('search failed: ' + r.status);
  const html = await r.text();
  const count = (html.match(/Record Count:\\s*(\\d+)/) || [])[1];
  const doc = new DOMParser().parseFromString(html, 'text/html');
  const table = doc.getElementById('results');
  const rows = table ? [...table.rows].map(tr => [...tr.cells].map(td => td.innerHTML)) : [];
  return {count: count ? +count : 0, rows};
}
"""

SHELBY_ADDR_RE = re.compile(r"^(.*?)\s*<br>\s*([A-Z .'-]+),\s*TN\s*(\d{5})", re.I)


def strip_tags(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(s or ""))).strip()


def shelby_first_last(name):
    """Shelby index is 'LAST FIRST MIDDLE' (suffix may follow the last name)."""
    if owner_type(name) != "person":
        return "", ""
    t = name.split()
    if len(t) < 2:
        return "", name.title()
    last = [t[0]]
    rest = t[1:]
    while rest and rest[0].upper() in NAME_SUFFIXES:
        last.append(rest.pop(0))
    return " ".join(rest).title(), " ".join(last).title()


def shelby_address(cell_html):
    """First real street address in the description cell (skips '0 MAIN ST' vacant lots)."""
    blocks = [b for b in re.split(r"(?:<br>\s*){2,}", str(cell_html or ""), flags=re.I) if b.strip()]
    found = []
    for b in blocks:
        m = SHELBY_ADDR_RE.search(re.sub(r"<br\s*/?>", "<br>", b.strip(), flags=re.I))
        if m:
            found.append((strip_tags(m.group(1)), m.group(2).strip().title(), m.group(3)))
    real = [f for f in found if not re.match(r"^0\s", f[0])]
    return (real or found or [("", "", "")])[0]


def shelby_rows_frame(rows):
    out = []
    for cells in rows:
        if len(cells) < 8:
            continue
        inst, rec_date = strip_tags(cells[0]), strip_tags(cells[1])
        if not re.match(r"\d{4}/\d{2}/\d{2}", rec_date):
            continue                                  # header rows / VOID instruments
        sellers = split_names(cells[2], sep=r"(?:<br\s*/?>\s*){2,}")
        buyers = split_names(cells[3], sep=r"(?:<br\s*/?>\s*){2,}")
        street, city, zip5 = shelby_address(cells[5])
        price = pd.to_numeric(re.sub(r"[^\d.]", "", strip_tags(cells[7])) or None, errors="coerce")
        first, last = shelby_first_last(first_person(buyers))
        out.append({
            "INSTRUMENT": inst,
            "DEED_DATE": datetime.strptime(rec_date, "%Y/%m/%d").date(),
            "DEED_TYPE": strip_tags(cells[4]),
            "BUYER_NAMES": " | ".join(buyers),
            "FIRST_NAME": first,
            "LAST_NAME": last,
            "BUYER_TYPE": owner_type(buyers[0]) if buyers else "unknown",
            "SELLER_NAMES": " | ".join(sellers),
            "SELLER_TYPE": owner_type(sellers[0]) if sellers else "unknown",
            "PROPERTY_ADDRESS": street,
            "CITY": city,
            "ZIP": zip5,
            "SALE_PRICE": price,
            "CROSS_REFERENCES": strip_tags(cells[6]),
        })
    return pd.DataFrame(out)


async def shelby_search(page, a, b, agent):
    """One search; splits the range in half if it hits the site's 1000-row cap."""
    res = await page.evaluate(SHELBY_JS, {"types": sorted(SHELBY_DOC_TYPES),
                                          "from": a.strftime("%m/%d/%Y"),   # must be zero-padded
                                          "to": b.strftime("%m/%d/%Y")})
    await pause()
    if res["count"] >= SHELBY_MAX_ROWS and a < b:
        mid = a + (b - a) // 2
        log(agent, f"{a} to {b} hit the {SHELBY_MAX_ROWS}-row cap, splitting")
        return (await shelby_search(page, a, mid, agent)
                + await shelby_search(page, mid + timedelta(days=1), b, agent))
    if res["count"] >= SHELBY_MAX_ROWS:
        log(agent, f"{a}: still {SHELBY_MAX_ROWS}+ rows on a single day, some are missing")
    log(agent, f"{a} to {b}: {res['count']} deeds")
    return res["rows"]


async def shelby_agent(browser, days):
    agent = "shelby"
    ctx = await browser.new_context(user_agent=USER_AGENT)
    page = await ctx.new_page()
    try:
        await page.goto(SHELBY_HOME, wait_until="domcontentloaded")   # sets the session
        await page.goto(SHELBY_SEARCH, wait_until="domcontentloaded")
        await page.wait_for_selector("#davidson_form", timeout=30_000)
        start, end = look_back(days)
        rows = []
        for a, b in date_chunks(start, end, 1):          # one day at a time (~30-80 deeds)
            rows += await shelby_search(page, a, b, agent)
    finally:
        await ctx.close()

    df = shelby_rows_frame(rows)
    if df.empty:
        log(agent, "nothing recorded in that window")
        return pd.DataFrame(columns=LEAD_COLS)
    df = df.drop_duplicates("INSTRUMENT")
    df["NEW_CONSTRUCTION"] = (df["SELLER_TYPE"].eq("entity")
                              & df["SELLER_NAMES"].str.upper().str.contains(BUILDER_RE, regex=True))
    df["COUNTY"], df["STATE"] = "SHELBY", "TN"
    # Latest deed per address.
    df = df.sort_values("DEED_DATE", ascending=False)
    has_addr = df["PROPERTY_ADDRESS"].ne("")
    df = pd.concat([df[has_addr].drop_duplicates(["PROPERTY_ADDRESS", "ZIP"]), df[~has_addr]])
    leads = finish_leads(df)
    # Shelby shows the sale price: a deed with no price is a gift / move into a living
    # trust / correction, not someone moving in, so it isn't a homeowner lead.
    leads["HOMEOWNER_LEAD"] = leads["HOMEOWNER_LEAD"] & (leads["SALE_PRICE"].fillna(0) > 0)
    leads = leads.sort_values(["HOMEOWNER_LEAD", "DEED_DATE"], ascending=[False, False])
    keep = LEAD_COLS + ["CROSS_REFERENCES"]
    leads[[c for c in keep if c in leads.columns]].to_csv(OUT_DIR / "shelby_recent_deeds.csv", index=False)
    log(agent, f"{len(rows)} rows -> {len(leads)} properties "
               f"({int(leads['HOMEOWNER_LEAD'].sum())} homeowner leads) -> shelby_recent_deeds.csv")
    return leads


# ============================================================
# SHARED
# ============================================================


def finish_leads(df):
    today = date.today()
    df = df.copy()
    df["DAYS_SINCE_DEED"] = df["DEED_DATE"].map(lambda d: (today - d).days if d else None)
    df["HOMEOWNER_LEAD"] = (df["BUYER_TYPE"].isin(["person", "trust"])
                            & df["PROPERTY_ADDRESS"].fillna("").ne(""))
    for col in LEAD_COLS:
        if col not in df.columns:
            df[col] = pd.NA
    return df.sort_values(["HOMEOWNER_LEAD", "DEED_DATE"], ascending=[False, False])


async def main(what, days=30, months=12, headful=False, out_dir="."):
    global OUT_DIR
    OUT_DIR = Path(out_dir)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        api = await p.request.new_context(user_agent=USER_AGENT)
        browser = None
        try:
            jobs = {}
            if what in ("desoto", "shelby", "deeds", "all"):
                browser = await p.chromium.launch(headless=not headful)
            if what in ("desoto", "deeds", "all"):
                jobs["desoto"] = desoto_agent(browser, api, days)
            if what in ("shelby", "deeds", "all"):
                jobs["shelby"] = shelby_agent(browser, days)
            if what in ("crime", "all"):
                jobs["crime"] = pull_crime(api, months)

            # Both county agents (and the crime pull) run at the same time.
            results = await asyncio.gather(*jobs.values(), return_exceptions=True)
            done = dict(zip(jobs, results))
            for name, res in done.items():
                if isinstance(res, Exception):
                    log(name, f"FAILED: {res!r}")

            counties = [done[k] for k in ("desoto", "shelby")
                        if k in done and isinstance(done[k], pd.DataFrame) and not done[k].empty]
            if len(counties) == 2:
                merged = pd.concat([c[LEAD_COLS] for c in counties], ignore_index=True)
                merged.sort_values(["HOMEOWNER_LEAD", "DEED_DATE"], ascending=[False, False]).to_csv(
                    OUT_DIR / "recent_deeds_all.csv", index=False)
                log("all", f"{len(merged)} properties from both counties -> recent_deeds_all.csv")
        finally:
            if browser:
                await browser.close()
            await api.dispose()

