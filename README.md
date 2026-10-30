# hotel-upload

Hotel price collection for the Rotelio pilot (accommodation data). The collector runs in
GitHub Actions twice a week and writes its results to a separate private data repository. The Rotelio app
repository does not run collectors or store datasets; this repository does.

## How it works

- **Source:** obilet, the same site the flight collector
  ([mertkiyar/rotelio-collector](https://github.com/mertkiyar/rotelio-collector)) uses. Like
  that collector, each query opens the real results page in a browser, as a visitor would,
  and keeps the hotel list the page itself loads. One page lists every hotel of the obilet
  location, so nothing is chosen or frozen in advance. (`request_mode: "fetch"` instead calls
  the list from one open page; it was refused by obilet after a few dozen queries.)
- **Regions:** the eight pilot regions of the flight data, same `region_id`s. Each has a
  centre and a radius in `hotels/obilet_regions.json`; only hotels inside it are kept.
- **Dates:** the flight window, i.e. departures on the next 30 days. A one-night stay for
  every check-in date in it, for 1 and for 2 adults (58 queries), so any stay inside the
  window is priced as the sum of its nights. Plus 5 real multi-night stays (3, 7 and 14
  nights, 2 adults) whose check-in day rotates every week; they measure how well the sum
  of nights matches the site's own price. 63 queries per region, 504 per run.
- **Refresh cycles:** a cycle (all 504 queries) starts every Tuesday and Friday. obilet limits
  how much it answers from GitHub's servers in a day, so the work is spread over nights:
  every night at 00:17 Turkish time one run sends at most `max_queries_per_run` (150)
  queries and the next night continues where it stopped. Queries are ordered by date across
  all regions (tomorrow for every region first), so a short night still covers every region's
  nearest dates. Check-ins that pass before they are reached are recorded as expired.
- **Pace:** as the flight collector: at least 30 s between queries, one browser session per
  run. A refusal (HTTP 401/403/429 or a human-verification page) ends that night's run;
  progress is kept and nothing retries around it.
- **Storage:** results go to a separate **private** data repository; this repository is
  public and holds code only. No Actions artifacts are uploaded.
- **Tiers:** `hotels/segments.py` turns a run into Economy / Standard / Premium hotels for
  given dates, using only hotels bookable on every night.

## Setup

1. Create a private repository for the data (e.g. `hotel-data`) with a README.
2. Create a fine-grained personal access token: *Only select repositories* = that data
   repository, *Repository permissions > Contents* = Read and write. Nothing else.
3. In this repository: Settings > Secrets and variables > Actions:
   - secret `HOTEL_DATA_TOKEN` = the token
   - variable `HOTEL_DATA_REPOSITORY` = `owner/hotel-data`
   - variable `COLLECTION_ENABLED` = `true` once a manual trial run has succeeded
4. The workflow refuses to run while this repository is private (to spend no private
   minutes) and refuses to write to a data repository that is not private.

Field definitions: [hotels/DATA_CONTRACT.md](hotels/DATA_CONTRACT.md).

## Running it by hand

Actions tab > **hotel-collect** > **Run workflow**. A manual run continues the current
cycle like a nightly one; for a short trial give one `region` and a small `budget` (e.g. 5).

Locally (Windows: `py` instead of `python`), from the `hotels` folder:

```
python -m pip install -r requirements.txt
python -m playwright install chromium
python obilet.py plan
python obilet.py collect --region antalya --budget 5
python segments.py --run out/2026-10-30 --region antalya --check-in 2026-11-14 --check-out 2026-11-17
python -m unittest discover -s tests -v
```

Excel for analysis (one sheet with every price and the hotel's facts, prices in TRY,
Turkish column names and a sheet explaining every column):

```
python export_excel.py --data ../../hotel-data/obilet            # newest cycle
python export_excel.py --data ../../hotel-data/obilet --cycle 2026-10-30
```

New location ids: `python obilet_locate.py "Place name"` (or the **obilet-locate** workflow).

## Older code

`collect_hotels.py`, `trivago.py`, `selection.py`, `cohort.py` and `pilot_hotels.json` are
the earlier Trivago collector. It is kept as a fallback and is not scheduled. The
`obilet_*probe.py` scripts were the access tests that led to the current design.
