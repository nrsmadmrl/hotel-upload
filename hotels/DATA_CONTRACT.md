# Hotel observation contract - version 1.0

Written by `obilet.py` to the private data repository, one folder per refresh cycle
(cycles start every Tuesday and Friday; `YYYY-MM-DD` is the cycle start, filled over
several nights):

```
obilet/YYYY-MM-DD/<region_id>/offers.csv.gz   one row per hotel per query (appended each night)
obilet/YYYY-MM-DD/<region_id>/hotels.csv      hotel facts seen in the cycle
obilet/YYYY-MM-DD/<region_id>/queries.csv     one row per query attempt (audit)
obilet/YYYY-MM-DD/<region_id>/manifest.json   planned / collected / remaining / expired, runs
obilet/latest.json                            newest cycle per region
```

`collection_date` and `observed_at` in the rows are the real observation day and time; a
cycle's rows can come from different nights. An expired query (its check-in passed before
it was reached) has no rows: missing, never zero.

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

The flight window (departures on the next 30 days, as the flight collector):

- `stay_kind=night`: a one-night stay for every check-in date whose check-out is inside the
  window, for 1 adult and for 2 adults. A user's stay inside the window is priced as the sum
  of its nights (`segments.py`); a hotel missing any night is left out, never estimated.
- `stay_kind=validation`: five real stays for 2 adults (3, 7 and 14 nights from a check-in
  3-9 days ahead; 3 and 7 nights from 14-20 days ahead). The check-in day moves every week,
  so all weekdays are covered over time. They measure how far the sum of one-night prices is
  from the site's own multi-night price (`segments.validation_report`). Hotels with a
  minimum stay appear only in these rows.

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
- `price_before_discount_minor`, `discount_ratio`: the site's strike-through price and its
  discount, only when the shown price is really reduced; blank otherwise.
- `is_refundable`, `supplier` (selling agency), `accommodation_tax_included` (as reported).
- `response_cached`, `response_expires_at`: obilet served a cached search; prices may be up
  to that cache's age.
- `analysis_eligible`: `true` only for priced rows whose offer length matches the stay.
- `source`: `obilet`. Prices are listing prices, not a checkout or booking.

## hotels.csv

`hotel_id`, `hotel_name`, `stars` (0 = no star class), `average_score` (0-10),
`comment_count`, `hotel_type_id`, `hotel_type_name` (`Şehir Otelleri` / `Tatil Otelleri`),
`facilities` (obilet's facility names, `;`-separated), `has_pool`, `has_parking`, `has_wifi`,
`is_beachfront`, `is_child_friendly`, `is_pet_friendly`, `is_accessible` (from those names;
`false` means obilet lists none, not a verified absence), `latitude`, `longitude`, `distance_to_region_center_km`,
`obilet_center_name`, `obilet_center_distance_km`, `photo_url` (obilet's main image) and
`photo_urls` (its gallery, up to 5, `;`-separated); images belong to obilet/the hotel: link,
do not re-host, `seen_available` / `seen_unavailable` (number of queries in this run).

## queries.csv

One row per query: `query_state` is `ok`, `empty` (no priced hotel in the radius),
`blocked` (HTTP 401/403/429), `http_error`, `not_json` or `dates_mismatch` (the response
echoed other dates; nothing kept). A refusal (`blocked`) or three other failures in a row end that night's run; the next
run continues the cycle; the run is
marked `stopped_early` in `manifest.json`. Blocks are not retried in a loop.

## Stay tiers

`segments.py` turns one run into the app's Economy / Standard / Premium tiers for a given
check-in, check-out and adult count: only hotels bookable on every night are used, the
exact multi-night price wins over the sum of nights, then guest-score/review floors and
price terciles. Tiers are relative to what is bookable on those dates, so they move with
the season; they are not star classes.
