# Hotel observation contract - version 1.0

Written by `obilet.py`, twice a week, to the `data` branch:

```
obilet/YYYY-MM-DD/<region_id>/offers.csv.gz   one row per hotel per query
obilet/YYYY-MM-DD/<region_id>/hotels.csv      hotel facts seen in that run
obilet/YYYY-MM-DD/<region_id>/queries.csv     one row per query (audit)
obilet/YYYY-MM-DD/<region_id>/manifest.json   counts, settings, stop reason
obilet/latest.json                            newest run per region
```

Conventions follow the Rotelio data rules: money is integer kurus, `null`/blank means
unknown (never free), observation time is kept apart from travel dates, and every price
keeps its room, guest, stay-length and meal conditions.

## Regions

`region_id` is the same key as the flight data (Rotelio `transportation/DATA_CONTRACT.md`):
`antalya` (Kaleiçi-Konyaaltı), `izmir_cesme`, `mugla_bodrum`, `mugla_fethiye`, `trabzon`,
`nevsehir`, `gaziantep`, `erzurum`. A hotel belongs to a region when its coordinates are
within `radius_km` of the region centre in `obilet_regions.json`; obilet's own location
usually covers a wider area. Join hotels to places by `region_id` and their own
coordinates; the destination airport is not the hotel location.

## Stays collected

Inside the flight horizon (60 days, as the flight collector):

- `stay_kind=night`: a one-night stay for every check-in date from tomorrow, for 1 and 2 adults.
- `stay_kind=validation`: a few real 3- and 7-night stays for 2 adults. They measure how far
  the sum of one-night prices is from the site's own multi-night price
  (`segments.validation_report`). Some hotels have a minimum stay and appear only in these.

## offers.csv.gz

- `schema_version`: `1.0`. One row per (`query_id`, `hotel_id`) in a run; join `hotels.csv` on
  `hotel_id` for the name and facts.
- `query_id`: region, check-in, check-out, adults, rooms, currency and schema version.
- `collection_date` (Europe/Istanbul day), `observed_at` (timestamp with offset).
- `region_id`, `hotel_id` (obilet id).
- `stay_kind`, `check_in_date`, `check_out_date`, `nights`, `lead_days` (check-in minus collection day).
- `search_adults` (1 or 2), `search_children` (0), `search_rooms` (1).
- `availability_status`: `available` (priced), `listed_no_price`, or `unavailable` (obilet lists
  the hotel as not bookable for these dates, e.g. closed for the season). Only `available` rows carry a price.
- `currency`: `TRY`.
- `price_total_minor`: integer kurus for the WHOLE stay, one room, all searched guests.
- `price_per_night_minor`: `price_total_minor / nights`, rounded to a kurus.
- `price_scope`: `stay_total_one_room_all_guests`.
- `board_category_id`, `board_category_name`, `board_name`: meal plan of the offer
  (`Sadece Oda`, `Kahvaltı Dahil`, `Yarım Pansiyon`, `Her Şey Dahil`, ...). The cheapest offer
  is shown, so the meal plan of one hotel can change between dates.
- `is_refundable`, `supplier` (selling agency), `accommodation_tax_included` (as reported).
- `response_cached`, `response_expires_at`: obilet served a cached search; prices may be up
  to that cache's age.
- `analysis_eligible`: `true` only for priced rows whose offer length matches the stay.
- `source`: `obilet`. Prices are listing prices, not a checkout or booking.

## hotels.csv

`hotel_id`, `hotel_name`, `stars` (0 = no star class), `average_score` (0-10),
`comment_count`, `hotel_type_id`, `latitude`, `longitude`, `distance_to_region_center_km`,
`obilet_center_name`, `obilet_center_distance_km`, `photo_url` (obilet's image; do not
re-host), `seen_available` / `seen_unavailable` (number of queries in this run).

## queries.csv

One row per query: `query_state` is `ok`, `empty` (no priced hotel in the radius),
`blocked` (HTTP 401/403/429), `http_error`, `not_json` or `dates_mismatch` (the response
echoed other dates; nothing kept). Three failures in a row stop the region; the run is
marked `stopped_early` in `manifest.json`. Blocks are not retried in a loop.

## Stay tiers

`segments.py` turns one run into the app's Economy / Standard / Premium tiers for a given
check-in, check-out and adult count: only hotels bookable on every night are used, the
exact multi-night price wins over the sum of nights, then guest-score/review floors and
price terciles. Tiers are relative to what is bookable on those dates, so they move with
the season; they are not star classes.
