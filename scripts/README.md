# Extraction Scripts

Three independent scripts produce the analysis-ready CSVs in `datasets/year_N/`, keyed
on `filename` (photos/videos) or `date` (steps). Joining them into one table is left for
the analysis phase. All three take a required `--year N`, writing into `datasets/year_N/`
by default -- this repo holds one such folder per year of photos (see `datasets/README.md`
for the schema).

- `metadata_extraction/extract_metadata.py` -> `datasets/year_N/metadata.csv`
- `vision_feature_extraction/extract_vision_features.py` -> `datasets/year_N/vision_features.csv`
- `gfit_extraction/extract_steps.py` -> `datasets/year_N/steps.csv`

## Prerequisites

```bash
pip install -r scripts/requirements.txt
```

### 1. Google Photos Picker API (OAuth 2.0)

Required by `extract_metadata.py` to fetch each photo's authoritative capture time,
camera make/model, and dimensions.

We use the **Picker API** rather than the older Library API's `mediaItems.list`, which
Google restricted in 2025 to only return media an app itself uploaded -- unusable for a
personal script reading an existing library. The Picker API works around that: it opens
a Google-hosted page where you pick which photos/videos this run gets metadata access
to, then returns full metadata for exactly those items.

**This means every run (not just the first) pauses for you to open a URL and select
photos in a browser** -- it's not a one-time setup step. In practice this maps well onto
an annual routine: point it at that year's folder, select that year's photos in the
picker, done.

**The output dataset is driven entirely by what you select in the picker, not by what's
in `--folder`.** If you select 200 items, `metadata.csv` has exactly 200 rows -- files in
`--folder` that weren't picked are excluded, even if they're otherwise perfectly valid
local photos. (The one exception is `--skip-photos-api`, a local-only testing mode that
ignores picker selection entirely -- see below.)

1. In [Google Cloud Console](https://console.cloud.google.com), enable the **Google
   Photos Picker API** on your project.
2. Under APIs & Services -> Credentials, create an **OAuth 2.0 Client ID** of type
   **Desktop app**.
3. Download the client secret JSON and save it as:
   `scripts/metadata_extraction/credentials/google_client_secret.json`
4. On first run, a browser window opens for you to sign in and authorize the app itself
   (one-time; token cached to `scripts/metadata_extraction/credentials/token.json`).
   Then, on every run, the script prints a picker URL -- open it, select the relevant
   photos/videos, and the script will detect completion and continue automatically.

### 2. Google Cloud Vision API (API key)

Required by `extract_vision_features.py` to detect labels, faces/emotions, text, and
dominant colors. This script outputs raw Vision data only (all labels with confidence
scores, per-face emotion likelihoods, dominant colors, text-detected flag) -- no derived
categories like "setting" or "mood"; that grouping is left for the analysis phase.

1. Enable the **Cloud Vision API** on the same project.
2. Create an API key under APIs & Services -> Credentials.
3. Set it as an environment variable:
   ```bash
   export GOOGLE_PHOTO_DATA_API_KEY=your_key_here
   ```

### 3. Google Fit Takeout export

Required by `extract_steps.py` to get step counts. Unlike the two APIs above, this is a
one-time manual export, not something the script fetches itself:

1. Go to [Google Takeout](https://takeout.google.com), select **Fit**, and request an
   export including the **Daily activity metrics** data (per-day CSVs with 15-minute
   step-count intervals, plus a summary CSV).
2. Unzip it into `raw_data/gfit/` so the script's default `--gfit-dir` -- pointing at
   `raw_data/gfit/Daily activity metrics/` -- finds it. Use `--gfit-dir` to point
   elsewhere.

## Usage

```bash
# Metadata: date, camera, orientation, calendar fields -> datasets/year_1/metadata.csv
python scripts/metadata_extraction/extract_metadata.py --folder raw_data/pics/year1/ --year 1

# Vision: labels+scores, face count, per-face emotions, text detection, dominant colors
# -> datasets/year_1/vision_features.csv (reads photo filenames from that year's metadata.csv)
python scripts/vision_feature_extraction/extract_vision_features.py --folder raw_data/pics/year1/ --year 1

# Steps: UTC-normalized daily total + time-of-day buckets -> datasets/year_1/steps.csv
# (scoped to the calendar years covered by that year's metadata.csv, e.g. 2023-2026)
python scripts/gfit_extraction/extract_steps.py --year 1
```

Both accept:
- `--folder` — a local directory or a `gs://bucket/prefix` GCS path
- `--year` — required; determines the `datasets/year_N/` output folder (and, for
  `extract_metadata.py`, the Picker API cache location) unless overridden below
- `--output` — output CSV path (default: `datasets/year_<year>/<script's file>.csv`)
- `--limit N` — process only the first N files, useful for a quick test run before
  committing to the full batch

`extract_metadata.py` additionally accepts:
- `--use-cache` — reuse that year's cached Picker API result instead of opening a new
  picker session (skips the browser step entirely; still picker-driven inclusion)
- `--skip-photos-api` — local-only testing mode: bypass the Picker API and process
  *every* file in `--folder` using EXIF/filename metadata only. This is not subject to
  the "rows == picker selection" guarantee since there's no picker selection in this mode.

`extract_vision_features.py` additionally accepts:
- `--metadata-csv` — which metadata.csv to read the photo filename list from (default:
  `datasets/year_<year>/metadata.csv`)

`extract_steps.py` differs slightly: `--folder` isn't used (no photos involved). It
accepts:
- `--year` — required; determines which `datasets/year_N/metadata.csv` to read the
  covered calendar years from, and the `datasets/year_N/steps.csv` output path
- `--metadata-csv` — override for the metadata.csv to read years from
- `--gfit-dir` — path to the Takeout "Daily activity metrics" folder (default:
  `raw_data/gfit/Daily activity metrics/`)
- `--output` — output CSV path (default: `datasets/year_<year>/steps.csv`)
- `--limit N` — only process the first N output dates, for testing

Every 15-minute interval is converted from its recorded local time + UTC offset to UTC
before being bucketed, the same rationale as `extract_metadata.py`'s EXIF-to-UTC
conversion — so `steps.csv`'s day boundaries and time-of-day buckets line up with
`metadata.csv`'s rather than drifting by a few hours depending on source. `total_steps`
is always the sum of that date's five bucket columns, not a separately reported figure.

## Logs

Each script writes its own `logs/extract.log` (per-file errors; extraction continues on
failure — bad rows get `N/A` values rather than aborting the run).
