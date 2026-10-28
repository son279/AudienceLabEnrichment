"""
audiencelab.shelby_lookup
-------------------------
Look up residential leads on the Shelby County Assessor property search
(street number + street name, no direction/suffix) and write one row per
matching parcel. Shared by every client; run it through run.py:

    python run.py baltz lookup [--limit N]
    python run.py mvt lookup   [--limit N]

A client can pass `signals_fn(data, year_built, permits) -> list[str]` to fill
the Signals column with its own sales hints (Baltz: concrete work).

Requires: pip install playwright pandas && python -m playwright install chromium
"""
import csv
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path


SEARCH_URL = "https://www.assessor.shelbycountytn.gov/propertySearch"

DIRECTIONS = {"N", "S", "E", "W", "NE", "NW", "SE", "SW",
              "NORTH", "SOUTH", "EAST", "WEST"}
SUFFIXES = {
    "ST", "STREET", "AVE", "AVENUE", "RD", "ROAD", "DR", "DRIVE", "LN", "LANE",
    "CT", "COURT", "CIR", "CIRCLE", "BLVD", "BOULEVARD", "PL", "PLACE", "WAY",
    "PKWY", "PARKWAY", "TRL", "TRAIL", "TER", "TERRACE", "CV", "COVE", "HWY",
    "HIGHWAY", "LOOP", "PT", "POINT", "SQ", "SQUARE", "XING", "CROSSING",
    "RDG", "RIDGE", "PIKE", "ALY", "ALLEY", "BND", "BEND", "CRK", "CREEK",
    "RUN", "PATH", "PASS", "ROW", "EXT", "EXPY",
}
FIELDS = ["Parcel ID", "Property Address", "Municipal Jurisdiction",
          "Subdivision Name", "Land Square Footage", "Acres",
          "Owner Name", "Owner Mailing Address", "Owner City/State/Zip"]

# Permit reason codes seen on the assessor site; unknown codes are kept as-is.
PERMIT_REASONS = {
    "ADD": "Addition", "SWP": "Swimming pool", "REM": "Remodel", "REP": "Repair",
    "ALT": "Alteration", "NEW": "New construction", "DEM": "Demolition",
    "RFG": "Roofing", "FNC": "Fence", "GAR": "Garage", "DEC": "Deck/patio",
    "PAT": "Patio", "DRV": "Driveway", "ACC": "Accessory building",
}
TODAY = date.today()


EXTRA_COLS = ["Class", "Year Built", "Total Appraisal", "Building Appraisal",
              "Extra Features", "Sales Count", "Last Sale Date", "Last Sale Price",
              "Last Sale Type", "Years Owned", "Permit Count", "Last Permit Date",
              "Last Permit Amount", "Last Permit Reason", "Last Permit Code",
              "Years Since Permit", "All Permits", "Signals"]


def split_address(address):
    """'6681 Hidden Fern Ln E' -> ('6681', 'Hidden Fern')."""
    parts = address.replace(",", " ").split()
    number, rest = parts[0], parts[1:]
    while len(rest) > 1 and rest[-1].upper().strip(".") in DIRECTIONS:
        rest.pop()
    if len(rest) > 1 and rest[-1].upper().strip(".") in SUFFIXES:
        rest.pop()
    while len(rest) > 1 and rest[0].upper().strip(".") in DIRECTIONS:
        rest.pop(0)  # leading direction, e.g. "N Main St"
    return number, " ".join(rest)


def clean(text):
    return re.sub(r"\s+", " ", text or "").strip()


def money(text):
    m = re.sub(r"[^\d.]", "", text or "")
    return float(m) if m else None


def to_date(text):
    try:
        return datetime.strptime(text.strip(), "%m/%d/%Y").date()
    except (ValueError, AttributeError):
        return None


def read_tables(page):
    """Return (key/value dict, list of header-based row lists). Tabs are hidden,
    so read textContent from the DOM instead of visible text."""
    return page.evaluate("""() => {
        const kv = {}, grids = [];
        const txt = e => (e.textContent || '').replace(/\\s+/g, ' ').trim();
        document.querySelectorAll('table').forEach(t => {
            const rows = [...t.querySelectorAll('tr')].map(r =>
                [...r.querySelectorAll('th,td')].map(txt));
            if (rows.length && rows.every(r => r.length === 2 && r[0].endsWith(':'))) {
                rows.forEach(r => kv[r[0].replace(/:$/, '').trim()] = r[1]);
            } else if (rows.length) {
                grids.push(rows);
            }
        });
        return [kv, grids];
    }""")


def grid(grids, first_header):
    for rows in grids:
        if rows[0] and rows[0][0].lower().startswith(first_header.lower()):
            head, seen, out = rows[0], set(), []
            for r in rows[1:]:
                if len(r) == len(head) and tuple(r) not in seen:  # site repeats rows
                    seen.add(tuple(r))
                    out.append(dict(zip(head, r)))
            return out
    return []


def scrape_details(page, signals_fn=None):
    kv, grids = read_tables(page)
    kv = {k.strip(): v for k, v in kv.items()}
    data = {f: kv.get(f, "") for f in FIELDS}

    sales = sorted(((to_date(r.get("Date of Sale")), money(r.get("Sales Price")),
                     r.get("Instrument Type", "")) for r in grid(grids, "Date of Sale")),
                   key=lambda x: x[0] or date.min, reverse=True)
    permits = sorted(((to_date(r.get("Date of Permit")), money(r.get("Amount of Permit")),
                       r.get("Reason", "")) for r in grid(grids, "Date of Permit")),
                     key=lambda x: x[0] or date.min, reverse=True)
    extras = grid(grids, "Card")

    year_built = kv.get("Year Built:", kv.get("Year Built", ""))
    data.update({
        "Class": kv.get("Class", ""),
        "Year Built": year_built,
        "Total Appraisal": kv.get("Total Appraisal", ""),
        "Building Appraisal": kv.get("Building Appraisal", ""),
        "Extra Features": "; ".join(sorted({clean(e.get("Type", "")) for e in extras})),
        "Sales Count": len(sales),
        "Permit Count": len(permits),
    })
    if sales and sales[0][0]:
        d, price, inst = sales[0]
        data.update({"Last Sale Date": d.isoformat(), "Last Sale Price": price,
                     "Last Sale Type": inst,
                     "Years Owned": round((TODAY - d).days / 365.25, 1)})
    if permits and permits[0][0]:
        d, amt, reason = permits[0]
        data.update({"Last Permit Date": d.isoformat(), "Last Permit Amount": amt,
                     "Last Permit Reason": PERMIT_REASONS.get(reason.upper(), reason) or "(no reason listed)",
                     "Last Permit Code": reason,
                     "Years Since Permit": round((TODAY - d).days / 365.25, 1)})
    data["All Permits"] = "; ".join(
        f"{d.isoformat() if d else '?'} {PERMIT_REASONS.get(r.upper(), r) or '(no reason listed)'}"
        f"{f' ${a:,.0f}' if a else ''}" for d, a, r in permits[:5])
    data["Signals"] = "; ".join(signals_fn(data, year_built, permits)) if signals_fn else ""
    return data


def lookup(page, number, name, signals_fn=None):
    page.goto(SEARCH_URL, timeout=60000)
    page.fill("#stNumber", number)
    page.fill("#stName", name)
    page.locator("form:has(#stNumber) button[type=submit]").click()
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1500)

    if "propertyDetails" in page.url:
        return [scrape_details(page, signals_fn)]

    # Multiple matches: open each result's parcel page.
    links = page.eval_on_selector_all(
        "a[href*='propertyDetails']", "els => els.map(e => e.href)")
    results = []
    for href in dict.fromkeys(links):
        page.goto(href, timeout=60000)
        page.wait_for_load_state("networkidle")
        results.append(scrape_details(page, signals_fn))
    return results


def run(input_csv, output_csv, limit=None, signals_fn=None):
    """Look up the first `limit` leads (None = all) of input_csv and write output_csv."""
    input_csv, output_csv = Path(input_csv), Path(output_csv)
    if not input_csv.exists():
        sys.exit(f"Input file '{input_csv}' not found. Run the client's clean step first.")
    if output_csv.resolve() == input_csv.resolve():
        sys.exit("Output file can't be the same as the input file.")
    with open(input_csv, newline="", encoding="utf-8-sig") as f:
        leads = list(csv.DictReader(f))
    leads = leads[:limit] if limit else leads
    print(f"Looking up {len(leads)} leads from {input_csv} -> {output_csv}")

    from playwright.sync_api import sync_playwright  # imported here so the rest of the
                                                     # package works without playwright
    out_rows = []
    with sync_playwright() as p:
        # Site blocks non-browser clients; headed Chromium is required.
        browser = p.chromium.launch(headless=False)
        page = browser.new_page()
        for i, lead in enumerate(leads, 1):
            if not (lead.get("ADDRESS") or "").strip():
                out_rows.append({"LEAD_NAME": f"{lead.get('FIRST_NAME', '')} {lead.get('LAST_NAME', '')}".strip(),
                                 "LEAD_ADDRESS": "", "MATCHES": 0, "ERROR": "no address"})
                print(f"{i:>2}. (no address): skipped")
                continue
            number, name = split_address(lead["ADDRESS"])
            row_base = {"LEAD_NAME": f"{lead.get('FIRST_NAME', '')} {lead.get('LAST_NAME', '')}".strip(),
                        "LEAD_ADDRESS": lead["ADDRESS"],
                        "SEARCH_NUMBER": number, "SEARCH_NAME": name}
            try:
                matches = lookup(page, number, name, signals_fn)
            except Exception as e:
                matches = []
                row_base["ERROR"] = str(e)[:100]
            if not matches or not any(matches):
                out_rows.append({**row_base, "MATCHES": 0})
                print(f"{i:>2}. {number} {name}: no results")
            for m in matches:
                if m:
                    out_rows.append({**row_base, "MATCHES": len(matches), **m})
                    print(f"{i:>2}. {number} {name}: {m.get('Property Address')} "
                          f"| {m.get('Owner Name')} | sale {m.get('Last Sale Date')} "
                          f"| permit {m.get('Last Permit Date')} {m.get('Last Permit Reason')}")
            time.sleep(1)
        browser.close()

    cols = ["LEAD_NAME", "LEAD_ADDRESS", "SEARCH_NUMBER", "SEARCH_NAME",
            "MATCHES"] + FIELDS + EXTRA_COLS + ["ERROR"]
    with open(output_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(out_rows)
    print(f"Saved {len(out_rows)} rows to {output_csv}")

