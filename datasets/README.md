# Datasets

Extraction outputs, one subfolder per year of photos. Each year's folder holds three CSVs
produced independently by the scripts in `scripts/` (see `scripts/README.md`):
`metadata.csv` and `vision_features.csv` join on `filename`; `steps.csv` is daily and
joins on the date portion of `metadata.csv`'s `date_taken`. Joining them into a single
table is left for the exploratory analysis phase, not done here.

## `year_1/`

Corresponds to `raw_data/pics/year1/` (435 files: photos + videos).

### `metadata.csv` (305 rows)

One row per file selected in the Google Photos Picker session for this year. Row count is
driven by that selection, not by what's in the local folder — see `scripts/README.md` for
why. Videos get date/calendar columns but not camera/orientation (Vision-independent,
sourced from the Picker API only).

| Column | Description |
|---|---|
| `filename` | |
| `media_type` | `photo` or `video` |
| `date_taken` | Capture time, normalized to UTC. Source priority: Google Photos API → EXIF (converted to UTC via its `OffsetTimeOriginal` tag) → filename pattern → `N/A`. Date-only sources (e.g. WhatsApp filenames) produce a date with no time component. |
| `date_source` | `google_photos_api` \| `exif` \| `filename` \| `N/A` |
| `day_of_week` | e.g. `Monday` |
| `month` | e.g. `March` |
| `week_of_year` | ISO week number |
| `time_of_day` | e.g. `3pm`; `N/A` if the date source has no time component |
| `time_of_day_category` | `morning` (6–11:59am) / `early afternoon` (12–2:59pm) / `late afternoon` (3–5:59pm) / `evening` (6–8:59pm) / `night` (9pm–5:59am); `N/A` under the same conditions as `time_of_day` |
| `camera_make`, `camera_model` | Photos only; `N/A` for videos |
| `orientation` | `portrait` \| `landscape` \| `unknown`; photos only |

### `vision_features.csv` (265 rows)

One row per **photo** row in `metadata.csv` (videos excluded — Vision only accepts
images). All fields are Vision's raw output; no derived/heuristic categories (grouping
labels into higher-level categories like "setting" or "occasion" is left for analysis).

| Column | Description |
|---|---|
| `filename` | |
| `label_1`..`label_15` | Labels Vision detected, in descending confidence order. Unused slots are `N/A`. |
| `label_1_score`..`label_15_score` | Confidence (0–1) for the corresponding label. |
| `people_count` | Number of faces detected. |
| `emotions` | Per face, raw `joy`/`sorrow`/`anger`/`surprise` likelihoods (`VERY_UNLIKELY`..`VERY_LIKELY`), comma-separated within a face, semicolon-separated between faces. `N/A` if no faces. |
| `contains_text` | `True`/`False` — whether Vision's text detection found any readable text. |
| `dominant_colors` | Top 3 dominant colors, hex, comma-separated. |

**4 rows have no vision features** (all columns except `filename` are `N/A`): these
filenames were selected in the Google Photos Picker session and so have a row in
`metadata.csv`, but the corresponding file was never downloaded into
`raw_data/pics/year1/`, so there were no image bytes to send to Vision.

- `Screenshot_20250624-235218~2.png`
- `PXL_20250828_041543150.jpg`
- `IMG_20260209_090736797_HDR.jpg`
- `IMG_20260209_164559961_HDR.jpg`

Separately, 2 rows (`IMG_8492.HEIC`, `IMG_8491.HEIC`) have real face/emotion/color/text
data but no labels (`label_1`..`label_15` all `N/A`) — Vision's label detection returned
nothing for these specific images. That's a genuine Vision API result, not a missing-file
or extraction problem like the 4 above.

### `steps.csv` (1,461 rows)

One row per calendar date, covering every date in the calendar years that appear in
`metadata.csv`'s `date_taken` (here: 2023–2026) — not just dates that have a photo.
Sourced from a one-time Google Fit Takeout export (`raw_data/gfit/`), not a live API.

| Column | Description |
|---|---|
| `date` | UTC calendar date (`YYYY-MM-DD`) |
| `day_of_week`, `month`, `week_of_year` | Derived from `date`, same conventions as `metadata.csv` |
| `total_steps` | Sum of the five bucket columns below |
| `steps_morning` / `steps_early_afternoon` / `steps_late_afternoon` / `steps_evening` / `steps_night` | Step counts in each time-of-day bucket, using the same hour boundaries as `metadata.csv`'s `time_of_day_category`. Each 15-minute Fit interval is converted from its recorded local time + UTC offset to UTC before bucketing, so these align with `metadata.csv`'s UTC-based buckets rather than local wall-clock time. |

**164 rows have `N/A` in every column except the calendar-derived ones**: `2026-07-21`
through `2026-12-31`, since the Fit export was taken on 2026-07-26 and only has data
through `2026-07-19`. (`2026-07-20` isn't `N/A` — it has partial real step data from the
UTC-shift of `2026-07-19`'s late-night local readings.) These aren't a data-quality
issue — just dates not yet reached when the export was made — and will need a fresh
export to fill in as the year progresses.
