# AudienceLab lead pipeline

AudienceLab turns purchased audience and company lists into cleaned, filtered,
and ranked leads for two clients:

- **Baltz Concrete** — residential, commercial, public-bid, and referral-partner leads.
- **MVT Security Cameras** — residential consumer and commercial leads.

The pipeline standardizes records, applies the client's service-area and lead
rules, and can enrich leads with Shelby County property records. MVT can also
collect recent deed and Memphis crime data. Scoring weights and service-area
settings are starting points; review them against each client's actual market
and past customers before using scores to make decisions.

## Requirements

- Python with `pip`
- Chromium installed through Playwright for assessor lookups and MVT public-records collection

From the repository root, create and activate a virtual environment, then install
the dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python -m playwright install chromium
```

The Playwright browser installation is needed for `lookup`,
`commercial-lookup`, and MVT `records` commands. Cleaning, enrichment, and
commercial scoring do not launch a browser.

## Input files

Put each client's audience CSV in `data/raw/` using these default names:

```text
data/raw/baltz_audience.csv
data/raw/mvt_audience.csv
```

The residential cleaner expects the AudienceLab export fields used by the
pipeline, including person name and address, personal and skip-trace address
data, `UUID`, `HOMEOWNER`, age, income, and net worth. Commercial steps also
use company and job fields such as `COMPANY_NAME`, `COMPANY_ADDRESS`,
`COMPANY_INDUSTRY`, `COMPANY_EMPLOYEE_COUNT`, and `JOB_TITLE`. Keep source
audience files and generated lead data private; they may contain personal
information.

Use `--audience FILE` to provide a different CSV path. The file must contain the
columns required by the selected client and step.

## Residential workflow

Run the three steps in order:

```powershell
python run.py baltz clean
python run.py baltz lookup
python run.py baltz enrich
```

Repeat with `mvt` for MVT. The lookup default is the first 20 residential leads;
use `--limit all` to process every lead:

```powershell
python run.py mvt lookup --limit all
```

You can run all three steps together. This also uses the default lookup limit
unless overridden:

```powershell
python run.py baltz all --limit all
python run.py mvt all --limit all
```

Cleaning filters the audience to the configured Memphis-area residential
market. Baltz's residential list is graded during enrichment after property
matching; MVT creates an initial audience-based score during cleaning and
re-grades leads during enrichment.

### Residential outputs

Outputs are written to `data/<client>/` by default:

| Client | Cleaned list | Enriched lead list | Other enrichment outputs |
|---|---|---|---|
| Baltz | `residential_leads.csv` | `residential_leads_enriched.csv` | `residential_investor_leads.csv`, `residential_excluded.csv` |
| MVT | `securitycam_residential_leads.csv` | `securitycam_consumer_leads.csv` | `securitycam_landlord_leads.csv`, `securitycam_excluded.csv` |

Both clients use `shelby_lookup_results.csv` for assessor lookup results.
Enrichment reads that file, so run `lookup` first. MVT crime points are
optional; if collected, enrichment reads `mpd_crime.csv`.

## Commercial workflow

Commercial scoring runs independently of the residential steps:

```powershell
python run.py baltz commercial
python run.py mvt commercial
```

To add property ownership and parcel signals to commercial scoring, run the
company-address lookup and rerun commercial scoring:

```powershell
python run.py baltz commercial-lookup --limit all
python run.py baltz commercial

python run.py mvt commercial-lookup --limit all
python run.py mvt commercial
```

The commercial lookup is optional. It uses the Shelby County assessor and
therefore only covers addresses in that assessor's area.

- **Baltz** writes `commercial_leads.csv` for direct buyers, including public
  bid leads, and `commercial_partner_leads.csv` for builders, contractors, and
  other referral/specification partners. Concrete and paving competitors are
  excluded.
- **MVT** writes `securitycam_commercial_leads.csv`.

Commercial lead criteria and scoring weights are in the `COMMERCIAL` sections
of each client's `config.py`.

## MVT public records (optional)

MVT can collect recent deeds from Shelby County, Tennessee and DeSoto County,
Mississippi, and property-crime counts from the Memphis data service:

```powershell
python run.py mvt records all --days 30 --months 12
```

Choose a specific collection with `all`, `deeds`, `desoto`, `shelby`, or
`crime`:

```powershell
python run.py mvt records crime --months 12
python run.py mvt records deeds --days 14
```

`deeds` collects from both counties. `all` collects deeds and crime data.
Outputs are written to `data/mvt/`, including `desoto_recent_deeds.csv`,
`shelby_recent_deeds.csv`, `recent_deeds_all.csv` (when both deed sources
return data), and `mpd_crime.csv`. Run the crime collection before enrichment
to include its optional scoring signal. Use `--headful` to show the browser
windows while records are collected.

These commands access public county and city services. Leave the request
pauses in place and avoid running overlapping collections.

## Paths and options

By default, the pipeline reads input from `data/raw/` and reads/writes step
files in `data/<client>/`. Override the output directory with `--data-dir`:

```powershell
python run.py baltz clean --data-dir .\tmp\baltz
```

Available shared options:

- `--audience FILE` — alternate input CSV for `clean` or `commercial`.
- `--data-dir DIR` — directory for step inputs and outputs.
- `--limit N` or `--limit all` — number of addresses to look up (default: 20).
- `--days N` — deed look-back period for MVT records (default: 30).
- `--months N` — crime look-back period for MVT records (default: 12).
- `--headful` — show browser windows for MVT records collection.

See all CLI options with:

```powershell
python run.py --help
```

## Project layout

```text
audiencelab/       Shared cleaning, filtering, parcel, assessor, and commercial logic
clients/baltz/     Baltz-specific cleaning, enrichment, commercial logic, and settings
clients/mvt/       MVT-specific cleaning, enrichment, commercial logic, records, and settings
data/raw/          Source audience CSVs
data/baltz/        Baltz intermediate and final files
data/mvt/          MVT intermediate and final files
run.py             Command-line entry point
```

## Adding a client

Use `clients/baltz/` or `clients/mvt/` as a starting point. Add the client's
cleaning, enrichment, and settings modules, then wire the client into
`CLIENTS` and `client_modules()` in `run.py`. Shared cleaning, filtering,
assessor lookup, and commercial helpers live in `audiencelab/`.
