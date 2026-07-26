#!/usr/bin/env python3
"""Extract per-file metadata (date, camera, orientation, calendar fields) for a folder of media.

Date is resolved in priority order: Google Photos Picker API -> EXIF -> filename
pattern -> N/A (no mtime fallback). See scripts/README.md for setup and usage.

The Picker API requires a one-time interactive step on every run (not just first run):
it opens a Google-hosted picker page where you select which photos/videos this run
should have metadata access to. This replaced the old mediaItems.list approach, which
Google restricted in 2025 to only return items an app itself uploaded.
"""

import argparse
import csv
import io
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image
from PIL.ExifTags import TAGS

import pillow_heif

pillow_heif.register_heif_opener()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import (  # noqa: E402
    basename,
    calendar_fields,
    list_media_files,
    media_type_for,
    read_bytes,
    time_of_day_category,
    time_of_day_label,
)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
CREDENTIALS_DIR = SCRIPT_DIR / "credentials"
CACHE_DIR = SCRIPT_DIR / "cache"
LOGS_DIR = SCRIPT_DIR / "logs"

SCOPES = ["https://www.googleapis.com/auth/photospicker.mediaitems.readonly"]
PICKER_API_BASE = "https://photospicker.googleapis.com/v1"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_POLL_TIMEOUT_SECONDS = 300.0

CSV_COLUMNS = [
    "filename",
    "media_type",
    "date_taken",
    "date_source",
    "day_of_week",
    "month",
    "week_of_year",
    "time_of_day",
    "time_of_day_category",
    "camera_make",
    "camera_model",
    "orientation",
]

# EXIF orientation values that involve a 90/270 degree rotation, requiring a
# width/height swap before deriving portrait vs. landscape. See plan doc for detail.
EXIF_ORIENTATION_SWAP = {5, 6, 7, 8}

WA_DATE_RE = re.compile(r"IMG-(\d{4})(\d{2})(\d{2})-WA\d+", re.IGNORECASE)
ANDROID_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})")

logger = logging.getLogger("extract_metadata")


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOGS_DIR / "extract.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def get_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    token_path = CREDENTIALS_DIR / "token.json"
    client_secret_path = CREDENTIALS_DIR / "google_client_secret.json"

    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not client_secret_path.exists():
                raise FileNotFoundError(
                    f"Missing OAuth client secret at {client_secret_path}. "
                    "See scripts/README.md for setup instructions."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())

    return creds


def parse_duration_seconds(duration_str, default: float) -> float:
    """Parses a protobuf JSON Duration string (e.g. '5s', '3.5s') to seconds."""
    if not duration_str:
        return default
    try:
        return float(str(duration_str).rstrip("s"))
    except ValueError:
        return default


def create_picker_session(creds) -> dict:
    import requests

    headers = {"Authorization": f"Bearer {creds.token}"}
    resp = requests.post(f"{PICKER_API_BASE}/sessions", headers=headers, json={}, timeout=30)
    resp.raise_for_status()
    return resp.json()


def poll_picker_session(creds, session_id: str, poll_interval: float, timeout: float) -> dict:
    import requests

    headers = {"Authorization": f"Bearer {creds.token}"}
    deadline = time.time() + timeout
    while True:
        resp = requests.get(f"{PICKER_API_BASE}/sessions/{session_id}", headers=headers, timeout=30)
        resp.raise_for_status()
        session = resp.json()
        if session.get("mediaItemsSet"):
            return session
        if time.time() >= deadline:
            raise TimeoutError(f"Timed out after {timeout:.0f}s waiting for photo selection in the browser.")
        time.sleep(poll_interval)


def list_picked_media_items(creds, session_id: str) -> list:
    import requests

    headers = {"Authorization": f"Bearer {creds.token}"}
    items = []
    page_token = None
    while True:
        params = {"sessionId": session_id, "pageSize": 100}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(f"{PICKER_API_BASE}/mediaItems", headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        items.extend(data.get("mediaItems", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return items


def delete_picker_session(creds, session_id: str) -> None:
    import requests

    try:
        headers = {"Authorization": f"Bearer {creds.token}"}
        requests.delete(f"{PICKER_API_BASE}/sessions/{session_id}", headers=headers, timeout=30)
    except Exception as e:
        logger.warning("Failed to clean up picker session %s: %s", session_id, e)


def fetch_picker_index(use_cache: bool, year: int) -> dict:
    """Runs a Google Photos Picker session and returns a dict keyed by filename.

    This always requires a human to open pickerUri in a browser and select
    photos/videos for this run -- it is not a one-time setup step, it happens on
    every invocation (unless --use-cache reuses a prior run's results).

    The cache is per-year so that running a later year's extraction doesn't
    overwrite an earlier year's cached selection.
    """
    cache_path = CACHE_DIR / f"year_{year}" / "photos_api_cache.json"
    if use_cache and cache_path.exists():
        logger.info("Using cached Picker API index at %s", cache_path)
        return json.loads(cache_path.read_text())

    creds = get_credentials()
    session = create_picker_session(creds)

    print("\nOpen this URL in a browser and select the photos/videos for this run:")
    print(f"  {session['pickerUri']}\n")
    print("Waiting for selection to complete...")

    polling_config = session.get("pollingConfig", {})
    poll_interval = parse_duration_seconds(polling_config.get("pollInterval"), DEFAULT_POLL_INTERVAL_SECONDS)
    timeout = parse_duration_seconds(polling_config.get("timeoutIn"), DEFAULT_POLL_TIMEOUT_SECONDS)

    session = poll_picker_session(creds, session["id"], poll_interval, timeout)
    items = list_picked_media_items(creds, session["id"])
    delete_picker_session(creds, session["id"])

    index = {}
    for item in items:
        fname = item.get("mediaFile", {}).get("filename")
        if fname:
            index[fname] = item

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(index, indent=2))
    logger.info("Fetched %d picked media items", len(index))
    print(f"Selected {len(index)} items.\n")
    return index


def parse_date_from_filename(fname: str):
    """Returns (date, source, has_time). WhatsApp filenames only encode a date, not a
    time of day, so has_time distinguishes that from Android filenames' full timestamp."""
    match = ANDROID_DATE_RE.match(fname)
    if match:
        y, mo, d, h, mi, s = map(int, match.groups())
        try:
            return datetime(y, mo, d, h, mi, s), "filename", True
        except ValueError:
            pass

    match = WA_DATE_RE.search(fname)
    if match:
        y, mo, d = map(int, match.groups())
        try:
            return datetime(y, mo, d), "filename", False
        except ValueError:
            pass

    return None, None, None


OFFSET_RE = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def parse_utc_offset(offset_str):
    """Parses an EXIF OffsetTime(Original) string like '-07:00' into a timezone object."""
    if not offset_str:
        return None
    match = OFFSET_RE.match(offset_str.strip())
    if not match:
        return None
    sign, hours, minutes = match.groups()
    delta = timedelta(hours=int(hours), minutes=int(minutes))
    return timezone(-delta if sign == "-" else delta)


def read_exif(path: str):
    """Best-effort EXIF read. Returns (date_taken, make, model, width, height, orientation)."""
    try:
        data = read_bytes(path)
        img = Image.open(io.BytesIO(data))
        width, height = img.size
        exif = img.getexif()
        if not exif:
            return None, None, None, width, height, None

        tagged = {TAGS.get(k, k): v for k, v in exif.items()}
        # DateTimeOriginal (and other capture-time tags) live in the nested "Exif" sub-IFD
        # (pointed to by tag 0x8769), not the flat IFD0 dict that getexif() returns directly.
        # Make/Model/Orientation are IFD0-level and don't need this.
        try:
            exif_sub_ifd = exif.get_ifd(0x8769)
            for k, v in exif_sub_ifd.items():
                tagged.setdefault(TAGS.get(k, k), v)
        except Exception:
            pass

        date_taken = None
        dto = tagged.get("DateTimeOriginal")
        if dto:
            try:
                date_taken = datetime.strptime(dto, "%Y:%m:%d %H:%M:%S")
            except ValueError:
                date_taken = None

        # If the camera also recorded its UTC offset (OffsetTimeOriginal, falling back to
        # OffsetTime), convert to UTC so this is directly comparable to the Google Photos
        # API's UTC timestamps -- otherwise the same photo's day-of-week/time-of-day could
        # come out different depending on which source supplied the date.
        if date_taken is not None:
            offset_str = tagged.get("OffsetTimeOriginal") or tagged.get("OffsetTime")
            offset = parse_utc_offset(offset_str)
            if offset is not None:
                date_taken = date_taken.replace(tzinfo=offset).astimezone(timezone.utc)

        return date_taken, tagged.get("Make"), tagged.get("Model"), width, height, tagged.get("Orientation")
    except Exception as e:
        logger.warning("Failed to read EXIF for %s: %s", path, e)
        return None, None, None, None, None, None


def derive_orientation(width, height, exif_orientation):
    if width is None or height is None:
        return "unknown"
    if exif_orientation in EXIF_ORIENTATION_SWAP:
        width, height = height, width
    return "portrait" if height > width else "landscape"


def derive_time_columns(dt: datetime):
    day_of_week, month, week_of_year = calendar_fields(dt)
    time_of_day = time_of_day_label(dt.hour)
    category = time_of_day_category(dt.hour)
    return day_of_week, month, week_of_year, time_of_day, category


def process_file(path: str, photos_index: dict) -> dict:
    fname = basename(path)
    m_type = media_type_for(fname)
    is_video = m_type == "video"

    row = {col: "N/A" for col in CSV_COLUMNS}
    row["filename"] = fname
    row["media_type"] = m_type

    date_taken = None
    date_source = None
    date_has_time = True  # API and EXIF timestamps always include a time; filename dates may not
    camera_make = None
    camera_model = None
    width = height = None
    exif_orientation = None

    api_item = photos_index.get(fname)
    if api_item:
        create_time = api_item.get("createTime")
        if create_time:
            try:
                date_taken = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
                date_source = "google_photos_api"
            except ValueError:
                pass
        if not is_video:
            file_meta = api_item.get("mediaFile", {}).get("mediaFileMetadata", {})
            camera_make = file_meta.get("cameraMake")
            camera_model = file_meta.get("cameraModel")
            width = int(file_meta["width"]) if file_meta.get("width") else None
            height = int(file_meta["height"]) if file_meta.get("height") else None

    # EXIF and filename-pattern fallbacks only apply to photos: Pillow can't read video
    # EXIF, and none of today's filename date patterns match video naming conventions
    # (VID_/PXL_/VID-...-WA...). Videos rely solely on the Picker API for a date.
    if not is_video:
        exif_date, exif_make, exif_model, exif_width, exif_height, exif_orientation = read_exif(path)

        if date_taken is None and exif_date is not None:
            date_taken = exif_date
            date_source = "exif"
        camera_make = camera_make or exif_make
        camera_model = camera_model or exif_model
        width = width if width is not None else exif_width
        height = height if height is not None else exif_height

        if date_taken is None:
            fname_date, src, has_time = parse_date_from_filename(fname)
            if fname_date is not None:
                date_taken, date_source, date_has_time = fname_date, src, has_time

    # No mtime fallback: filesystem mtime reflects when the file was last copied/moved,
    # not when the photo was taken, so it's not a trustworthy date source. If the API,
    # EXIF, and filename all come up empty, date fields are left as N/A.

    if not is_video:
        row["orientation"] = derive_orientation(width, height, exif_orientation)
        row["camera_make"] = camera_make or "N/A"
        row["camera_model"] = camera_model or "N/A"

    if date_taken is not None:
        row["date_taken"] = date_taken.date().isoformat() if not date_has_time else date_taken.isoformat()
        row["date_source"] = date_source
        dow, month, woy, tod, category = derive_time_columns(date_taken)
        row["day_of_week"] = dow
        row["month"] = month
        row["week_of_year"] = woy
        if date_has_time:
            row["time_of_day"] = tod
            row["time_of_day_category"] = category
        # else: leave time_of_day/time_of_day_category as N/A -- a date-only source
        # (e.g. WhatsApp filenames) doesn't tell us the actual hour

    return row


def build_api_only_row(fname: str, api_item: dict) -> dict:
    """Builds a row from Picker API data alone, for a picked item with no matching local
    file (e.g. it was selected in the picker but isn't present in --folder)."""
    row = {col: "N/A" for col in CSV_COLUMNS}
    row["filename"] = fname
    is_video = api_item.get("type") == "VIDEO"
    row["media_type"] = "video" if is_video else "photo"

    if not is_video:
        file_meta = api_item.get("mediaFile", {}).get("mediaFileMetadata", {})
        width = int(file_meta["width"]) if file_meta.get("width") else None
        height = int(file_meta["height"]) if file_meta.get("height") else None

        row["camera_make"] = file_meta.get("cameraMake") or "N/A"
        row["camera_model"] = file_meta.get("cameraModel") or "N/A"
        # No EXIF Orientation tag available without the local file, so no rotation correction.
        row["orientation"] = derive_orientation(width, height, None)

    create_time = api_item.get("createTime")
    if create_time:
        try:
            date_taken = datetime.fromisoformat(create_time.replace("Z", "+00:00"))
        except ValueError:
            date_taken = None
        if date_taken is not None:
            row["date_taken"] = date_taken.isoformat()
            row["date_source"] = "google_photos_api"
            dow, month, woy, tod, category = derive_time_columns(date_taken)
            row["day_of_week"] = dow
            row["month"] = month
            row["week_of_year"] = woy
            row["time_of_day"] = tod
            row["time_of_day_category"] = category

    return row


def main():
    parser = argparse.ArgumentParser(
        description="Extract Google Photos + EXIF metadata for the files selected in a Picker session."
    )
    parser.add_argument("--folder", required=True, help="Local folder path or gs:// bucket prefix")
    parser.add_argument("--year", type=int, required=True, help="Year number for this batch (e.g. 1) -- determines the datasets/year_N/ output folder and picker cache location, unless --output overrides it")
    parser.add_argument("--output", default=None, help="Output CSV path (default: datasets/year_<year>/metadata.csv)")
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N picked items (for testing)")
    parser.add_argument(
        "--use-cache", action="store_true", help="Reuse the cached Picker API result instead of running a new picker session"
    )
    parser.add_argument(
        "--skip-photos-api",
        action="store_true",
        help=(
            "Local-only testing mode: bypass the Picker API and process every file in --folder using "
            "EXIF/filename metadata only. Unlike the default mode, this ignores picker selection entirely "
            "and is not subject to the 'dataset rows == picker selection count' guarantee."
        ),
    )
    args = parser.parse_args()

    setup_logging()

    output_path = Path(args.output) if args.output else PROJECT_ROOT / "datasets" / f"year_{args.year}" / "metadata.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    if args.skip_photos_api:
        logger.info("Skipping Picker API per --skip-photos-api flag; processing all local files")
        files = list_media_files(args.folder)
        if args.limit:
            files = files[: args.limit]

        for path in files:
            try:
                rows.append(process_file(path, {}))
            except Exception as e:
                logger.error("Failed to process %s: %s", path, e)
                fname = basename(path)
                row = {col: "N/A" for col in CSV_COLUMNS}
                row["filename"] = fname
                row["media_type"] = media_type_for(fname)
                rows.append(row)
    else:
        # The dataset is driven entirely by what was picked -- a row exists if and only if
        # its filename was selected in the Picker session, regardless of what else is in --folder.
        try:
            photos_index = fetch_picker_index(args.use_cache, args.year)
        except Exception as e:
            logger.error("Could not complete Picker API session: %s", e)
            print(f"ERROR: Picker API session failed ({e}); no selection to build a dataset from.", file=sys.stderr)
            sys.exit(1)

        picked_filenames = list(photos_index.keys())
        if args.limit:
            picked_filenames = picked_filenames[: args.limit]

        local_by_name = {basename(p): p for p in list_media_files(args.folder)}

        for fname in picked_filenames:
            try:
                local_path = local_by_name.get(fname)
                if local_path is not None:
                    rows.append(process_file(local_path, photos_index))
                else:
                    logger.warning("Picked item %s has no matching file in %s; using API data only", fname, args.folder)
                    rows.append(build_api_only_row(fname, photos_index[fname]))
            except Exception as e:
                logger.error("Failed to process %s: %s", fname, e)
                row = {col: "N/A" for col in CSV_COLUMNS}
                row["filename"] = fname
                row["media_type"] = media_type_for(fname)
                rows.append(row)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
