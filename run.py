"""
AudienceLab lead pipeline: one entry point for every client.

Steps (run in order):
    python run.py <client> clean  [--audience FILE]   1. clean + filter -> residential_leads
    python run.py <client> lookup [--limit N|all]     2. Shelby assessor lookup (opens Chrome)
    python run.py <client> enrich                     3. grade with property data -> final list
    python run.py <client> all    [--limit N]         1 + 2 + 3

Commercial (both clients):
    python run.py <client> commercial [--audience FILE]     score commercial leads
    python run.py <client> commercial-lookup [--limit N|all]  optional: assessor lookup on company
                                                       addresses, then rerun `commercial`
        baltz -> commercial_leads.csv (direct + public bid) and commercial_partner_leads.csv
        mvt   -> securitycam_commercial_leads.csv

MVT only:
    python run.py mvt records {all,deeds,desoto,shelby,crime} [--days 30] [--months 12] [--headful]
        recent deeds + MPD crime; run `records crime` before `enrich` for crime points

Clients: baltz, mvt. Raw audience files live in data/raw/, every output in
data/<client>/. Use --data-dir to point at another folder (e.g. for a test run).
"""
import argparse
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CLIENTS = ("baltz", "mvt")


def client_modules(name):
    if name == "baltz":
        from clients.baltz import clean, config, enrich
        return config, clean, enrich, enrich.lookup_signals
    from clients.mvt import clean, config, enrich
    return config, clean, enrich, None


def step_clean(name, data, audience):
    config, clean, _, _ = client_modules(name)
    audience = Path(audience) if audience else ROOT / "data" / "raw" / config.AUDIENCE_FILE
    clean.run(audience, data / config.RESIDENTIAL_LEADS)


def step_lookup(name, data, limit):
    from audiencelab import shelby_lookup
    config, _, _, signals = client_modules(name)
    shelby_lookup.run(data / config.RESIDENTIAL_LEADS, data / config.LOOKUP_RESULTS,
                      limit=limit, signals_fn=signals)


def step_enrich(name, data):
    config, _, enrich, _ = client_modules(name)
    if name == "baltz":
        enrich.run(data / config.RESIDENTIAL_LEADS, data / config.LOOKUP_RESULTS,
                   data / config.ENRICHED_LEADS, data / config.INVESTOR_LEADS,
                   data / config.EXCLUDED_LEADS)
    else:
        enrich.run(data / config.RESIDENTIAL_LEADS, data / config.LOOKUP_RESULTS,
                   data / config.CRIME_FILE, data / config.CONSUMER_LEADS,
                   data / config.LANDLORD_LEADS, data / config.EXCLUDED_LEADS)


DEFAULT_LOOKUP_LIMIT = 20   # same default as the old shelby_lookup.py; use --limit all for everything


def step_commercial(name, data, audience):
    if name == "baltz":
        from clients.baltz import commercial, config
    else:
        from clients.mvt import commercial, config
    audience = Path(audience) if audience else ROOT / "data" / "raw" / config.AUDIENCE_FILE
    lookup = (data / config.COMMERCIAL_LOOKUP_INPUT, data / config.COMMERCIAL_LOOKUP_RESULTS)
    if name == "baltz":
        commercial.run(audience, data / config.COMMERCIAL_LEADS,
                       data / config.COMMERCIAL_PARTNER_LEADS, *lookup)
    else:
        commercial.run(audience, data / config.CRIME_FILE, data / config.COMMERCIAL_LEADS, *lookup)


def step_commercial_lookup(name, data, limit):
    from audiencelab import shelby_lookup
    if name == "baltz":
        from clients.baltz import config
    else:
        from clients.mvt import config
    shelby_lookup.run(data / config.COMMERCIAL_LOOKUP_INPUT, data / config.COMMERCIAL_LOOKUP_RESULTS,
                      limit=limit)


def parse_limit(text):
    if text is None:
        return DEFAULT_LOOKUP_LIMIT
    if text.lower() == "all":
        return None
    return int(text) or None          # 0 also means all


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("client", choices=CLIENTS)
    ap.add_argument("step", choices=["clean", "lookup", "enrich", "all", "records",
                                         "commercial", "commercial-lookup"])
    ap.add_argument("what", nargs="?", choices=["all", "deeds", "desoto", "shelby", "crime"],
                    help="records only: which public records to pull")
    ap.add_argument("--audience", help="clean: audience file (default data/raw/<client>_audience.csv)")
    ap.add_argument("--limit", help="lookup: first N leads, or 'all' (default 20)")
    ap.add_argument("--data-dir", help="where step files are read/written (default data/<client>)")
    ap.add_argument("--days", type=int, default=30, help="records: deed look-back in days")
    ap.add_argument("--months", type=int, default=12, help="records: crime look-back in months")
    ap.add_argument("--headful", action="store_true", help="records: show the browser windows")
    args = ap.parse_args()

    data = Path(args.data_dir) if args.data_dir else ROOT / "data" / args.client
    data.mkdir(parents=True, exist_ok=True)

    if args.step == "records":
        if args.client != "mvt":
            ap.error("records is only set up for mvt")
        from clients.mvt import public_records
        asyncio.run(public_records.main(args.what or "all", args.days, args.months,
                                        args.headful, data))
        return
    if args.step in ("commercial", "commercial-lookup"):
        if args.step == "commercial":
            step_commercial(args.client, data, args.audience)
        else:
            step_commercial_lookup(args.client, data, parse_limit(args.limit))
        return
    if args.step in ("clean", "all"):
        step_clean(args.client, data, args.audience)
    if args.step in ("lookup", "all"):
        step_lookup(args.client, data, parse_limit(args.limit))
    if args.step in ("enrich", "all"):
        step_enrich(args.client, data)


if __name__ == "__main__":
    main()
