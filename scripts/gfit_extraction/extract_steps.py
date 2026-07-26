#!/usr/bin/env python3
"""Extract UTC-normalized daily + time-of-day step counts from a Google Fit
Takeout export, scoped to the calendar years covered by a year_N/metadata.csv.

Reads raw_data/gfit/Daily activity metrics/<date>.csv (a one-time Takeout
export, not re-fetched via any API): 15-minute-interval rows, each carrying
its own local UTC offset. Every interval is converted to UTC before being
bucketed -- same rationale as extract_metadata.py's EXIF-to-UTC conversion,
so day boundaries and time-of-day buckets here are directly comparable to
metadata.csv's. total_steps is the sum of the buckets, not a separately
reported figure, so the two always agree.

Only used to bound the date range -- no join with metadata.csv happens here;
that's left for the exploratory analysis phase. See scripts/README.md.
"""

import argparse
import csv
import datetime
import logging
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
LOGS_DIR = SCRIPT_DIR / "logs"

DEFAULT_GFIT_DIR = PROJECT_ROOT / "raw_data" / "gfit" / "Daily activity metrics"

CSV_COLUMNS = [
    "date",
    "day_of_week",
    "month",
    "week_of_year",
    "total_steps",
    "steps_morning",
    "steps_early_afternoon",
    "steps_late_afternoon",
    "steps_evening",
    "steps_night",
]

BUCKET_COLUMNS = ["steps_morning", "steps_early_afternoon", "steps_late_afternoon", "steps_evening", "steps_night"]

logger = logging.getLogger("extract_steps")


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOGS_DIR / "extract.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def years_from_metadata(metadata_csv_path: Path) -> list:
    years = set()
    with open(metadata_csv_path, newline="") as f:
        for row in csv.DictReader(f):
            dt = row.get("date_taken", "")
            if dt and dt != "N/A":
                years.add(int(dt[:4]))
    return sorted(years)


def bucket_for_utc_hour(hour: int) -> str:
    if hour >= 21 or hour < 6:
        return "steps_night"
    if hour < 12:
        return "steps_morning"
    if hour < 15:
        return "steps_early_afternoon"
    if hour < 18:
        return "steps_late_afternoon"
    return "steps_evening"


def daterange(start: datetime.date, end: datetime.date):
    d = start
    while d <= end:
        yield d
        d += datetime.timedelta(days=1)


def accumulate_utc_buckets(gfit_dir: Path, local_start: datetime.date, local_end: datetime.date) -> dict:
    """Reads local day files in [local_start, local_end], converts every interval to
    UTC via its own offset, and accumulates step sums keyed by UTC date. A UTC date
    only appears in the returned dict if at least one interval (blank or not) mapped
    to it -- that's what distinguishes a real zero from N/A (no source data) later."""
    utc_days = {}
    for d in daterange(local_start, local_end):
        day_csv_path = gfit_dir / f"{d.isoformat()}.csv"
        if not day_csv_path.exists():
            logger.warning("No intraday file for local date %s", d.isoformat())
            continue
        with open(day_csv_path, newline="") as f:
            for row in csv.DictReader(f):
                start = row.get("Start time", "")
                steps = row.get("Step count", "").strip()
                if not start:
                    continue
                try:
                    local_dt = datetime.datetime.fromisoformat(f"{d.isoformat()}T{start}")
                except ValueError:
                    logger.warning("Unparseable Start time %r in %s", start, day_csv_path)
                    continue
                utc_dt = local_dt.astimezone(datetime.timezone.utc)
                utc_date = utc_dt.date().isoformat()
                bucket = bucket_for_utc_hour(utc_dt.hour)

                entry = utc_days.setdefault(utc_date, {col: 0 for col in BUCKET_COLUMNS})
                if steps:
                    entry[bucket] += int(steps)
    return utc_days


def main():
    parser = argparse.ArgumentParser(
        description="Extract UTC-normalized daily + time-of-day step counts, scoped to metadata.csv's photo years."
    )
    parser.add_argument("--year", type=int, required=True, help="Project year number (e.g. 1) -- determines datasets/year_N/ paths")
    parser.add_argument(
        "--metadata-csv",
        default=None,
        help="metadata.csv to read covered calendar years from (default: datasets/year_<year>/metadata.csv)",
    )
    parser.add_argument(
        "--gfit-dir",
        default=str(DEFAULT_GFIT_DIR),
        help=f"Path to the Takeout 'Daily activity metrics' folder (default: {DEFAULT_GFIT_DIR})",
    )
    parser.add_argument("--output", default=None, help="Output CSV path (default: datasets/year_<year>/steps.csv)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N output dates (for testing)")
    args = parser.parse_args()

    setup_logging()

    year_dir = PROJECT_ROOT / "datasets" / f"year_{args.year}"
    metadata_csv_path = Path(args.metadata_csv) if args.metadata_csv else year_dir / "metadata.csv"
    if not metadata_csv_path.exists():
        print(f"ERROR: {metadata_csv_path} not found. Run extract_metadata.py first.", file=sys.stderr)
        sys.exit(1)

    target_years = years_from_metadata(metadata_csv_path)
    if not target_years:
        print(f"ERROR: no dated rows found in {metadata_csv_path}.", file=sys.stderr)
        sys.exit(1)

    gfit_dir = Path(args.gfit_dir)
    range_start = datetime.date(target_years[0], 1, 1)
    range_end = datetime.date(target_years[-1], 12, 31)
    # 1-day buffer on each side to catch UTC-shifted spillover across the range boundary
    utc_days = accumulate_utc_buckets(
        gfit_dir, range_start - datetime.timedelta(days=1), range_end + datetime.timedelta(days=1)
    )

    output_dates = [d for d in daterange(range_start, range_end) if d.year in target_years]
    if args.limit:
        output_dates = output_dates[: args.limit]

    rows = []
    for d in output_dates:
        date_str = d.isoformat()
        row = {
            "date": date_str,
            "day_of_week": d.strftime("%A"),
            "month": d.strftime("%B"),
            "week_of_year": d.isocalendar()[1],
        }
        buckets = utc_days.get(date_str)
        if buckets is None:
            row["total_steps"] = "N/A"
            for col in BUCKET_COLUMNS:
                row[col] = "N/A"
        else:
            row.update(buckets)
            row["total_steps"] = sum(buckets.values())
        rows.append(row)

    output_path = Path(args.output) if args.output else year_dir / "steps.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_path} (years: {target_years})")


if __name__ == "__main__":
    main()
