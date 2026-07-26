#!/usr/bin/env python3
"""Extract Google Cloud Vision labels/faces/text/colors for photos in a folder.

The set of photos processed is read from metadata.csv (--metadata-csv), not by
walking --folder directly -- this keeps vision_features.csv aligned with
metadata.csv's picker-driven selection instead of silently including local
photos that were never part of that selection. Videos are skipped entirely (no
rows written). Images are converted to JPEG and downscaled before upload (see
prepare_image_for_upload). See scripts/README.md for setup and usage.
"""

import argparse
import base64
import csv
import io
import logging
import os
import sys
import time
from pathlib import Path

import pillow_heif
from PIL import Image

pillow_heif.register_heif_opener()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import basename, list_media_files, media_type_for, read_bytes  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
LOGS_DIR = SCRIPT_DIR / "logs"

VISION_API_URL = "https://vision.googleapis.com/v1/images:annotate"

MAX_LABELS = 15  # matches the LABEL_DETECTION maxResults requested below

# Raw data only -- no derived/heuristic columns (setting, occasion_type, mood). Those are
# left for the exploratory analysis phase, done from these raw labels/scores/emotions.
CSV_COLUMNS = ["filename"]
for _i in range(1, MAX_LABELS + 1):
    CSV_COLUMNS += [f"label_{_i}", f"label_{_i}_score"]
CSV_COLUMNS += ["people_count", "emotions", "dominant_colors", "contains_text"]

# Vision's four per-face emotion likelihood fields (there are other non-emotion
# likelihoods too, e.g. blurredLikelihood, headwearLikelihood -- excluded here).
EMOTION_FIELDS = ["joyLikelihood", "sorrowLikelihood", "angerLikelihood", "surpriseLikelihood"]

logger = logging.getLogger("extract_vision_features")


def setup_logging():
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        filename=str(LOGS_DIR / "extract.log"),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


MAX_UPLOAD_DIMENSION = 2048  # generous for label/face/text detection accuracy


def prepare_image_for_upload(fname: str, image_bytes: bytes) -> bytes:
    """Converts HEIC to JPEG (Vision doesn't accept HEIC) and downscales anything
    larger than MAX_UPLOAD_DIMENSION on its longest side. Multi-MB originals showed
    a much higher rate of intermittent connection failures against the Vision API
    during testing; shrinking the payload avoids that as well as speeding uploads."""
    img = Image.open(io.BytesIO(image_bytes))
    if img.format == "JPEG" and max(img.size) <= MAX_UPLOAD_DIMENSION:
        return image_bytes

    img = img.convert("RGB")
    if max(img.size) > MAX_UPLOAD_DIMENSION:
        img.thumbnail((MAX_UPLOAD_DIMENSION, MAX_UPLOAD_DIMENSION), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def rgb_to_hex(color: dict) -> str:
    r = int(color.get("red", 0))
    g = int(color.get("green", 0))
    b = int(color.get("blue", 0))
    return f"#{r:02x}{g:02x}{b:02x}"


def analyze_image(image_bytes: bytes, api_key: str, max_attempts: int = 4) -> dict:
    import requests

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    body = {
        "requests": [
            {
                "image": {"content": b64},
                "features": [
                    {"type": "LABEL_DETECTION", "maxResults": 15},
                    {"type": "FACE_DETECTION", "maxResults": 20},
                    {"type": "TEXT_DETECTION", "maxResults": 1},
                    {"type": "IMAGE_PROPERTIES"},
                ],
            }
        ]
    }
    # Transient connection errors (observed: intermittent SSL errors against this API)
    # are retried with backoff; a real HTTP error response still raises after retries.
    for attempt in range(max_attempts):
        try:
            resp = requests.post(f"{VISION_API_URL}?key={api_key}", json=body, timeout=60)
            resp.raise_for_status()
            return resp.json()["responses"][0]
        except requests.exceptions.RequestException:
            if attempt == max_attempts - 1:
                raise
            time.sleep(2**attempt)


def extract_columns_from_response(resp: dict) -> dict:
    row = {}

    labels = resp.get("labelAnnotations", [])
    for i in range(MAX_LABELS):
        n = i + 1
        if i < len(labels):
            row[f"label_{n}"] = labels[i].get("description", "N/A")
            row[f"label_{n}_score"] = round(labels[i].get("score", 0), 4)
        else:
            row[f"label_{n}"] = "N/A"
            row[f"label_{n}_score"] = "N/A"

    faces = resp.get("faceAnnotations", [])
    row["people_count"] = len(faces)
    row["emotions"] = (
        "; ".join(",".join(f"{field[:-len('Likelihood')]}={face.get(field, 'UNKNOWN')}" for field in EMOTION_FIELDS) for face in faces)
        if faces
        else "N/A"
    )

    row["contains_text"] = bool(resp.get("textAnnotations"))

    colors = resp.get("imagePropertiesAnnotation", {}).get("dominantColors", {}).get("colors", [])
    top_colors = sorted(colors, key=lambda c: c.get("score", 0), reverse=True)[:3]
    row["dominant_colors"] = ",".join(rgb_to_hex(c["color"]) for c in top_colors) if top_colors else "N/A"

    return row


def read_photo_filenames_from_metadata(metadata_csv_path: Path) -> list:
    with open(metadata_csv_path, newline="") as f:
        rows = list(csv.DictReader(f))
    return [r["filename"] for r in rows if r.get("media_type") == "photo"]


def main():
    parser = argparse.ArgumentParser(
        description="Extract Google Cloud Vision features for the photos listed in metadata.csv."
    )
    parser.add_argument("--folder", required=True, help="Local folder path or gs:// bucket prefix")
    parser.add_argument("--year", type=int, required=True, help="Year number for this batch (e.g. 1) -- determines datasets/year_N/ paths for --output and --metadata-csv defaults")
    parser.add_argument("--output", default=None, help="Output CSV path (default: datasets/year_<year>/vision_features.csv)")
    parser.add_argument(
        "--metadata-csv",
        default=None,
        help="metadata.csv to read the photo filename list from (default: datasets/year_<year>/metadata.csv)",
    )
    parser.add_argument("--limit", type=int, default=None, help="Only process the first N photos (for testing)")
    args = parser.parse_args()

    setup_logging()

    api_key = os.environ.get("GOOGLE_PHOTO_DATA_API_KEY")
    if not api_key:
        print("ERROR: GOOGLE_PHOTO_DATA_API_KEY environment variable is not set.", file=sys.stderr)
        sys.exit(1)

    year_dir = PROJECT_ROOT / "datasets" / f"year_{args.year}"
    metadata_csv_path = Path(args.metadata_csv) if args.metadata_csv else year_dir / "metadata.csv"
    if not metadata_csv_path.exists():
        print(f"ERROR: {metadata_csv_path} not found. Run extract_metadata.py first.", file=sys.stderr)
        sys.exit(1)

    photo_filenames = read_photo_filenames_from_metadata(metadata_csv_path)
    if args.limit:
        photo_filenames = photo_filenames[: args.limit]

    local_by_name = {basename(p): p for p in list_media_files(args.folder) if media_type_for(basename(p)) == "photo"}

    output_path = Path(args.output) if args.output else year_dir / "vision_features.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    for fname in photo_filenames:
        local_path = local_by_name.get(fname)
        if local_path is None:
            logger.warning("%s is in metadata.csv but has no local file in %s; writing N/A row", fname, args.folder)
            row = {col: "N/A" for col in CSV_COLUMNS}
            row["filename"] = fname
            rows.append(row)
            continue
        try:
            image_bytes = prepare_image_for_upload(fname, read_bytes(local_path))
            resp = analyze_image(image_bytes, api_key)
            row = {"filename": fname, **extract_columns_from_response(resp)}
        except Exception as e:
            logger.error("Failed to analyze %s: %s", local_path, e)
            row = {col: "N/A" for col in CSV_COLUMNS}
            row["filename"] = fname
        rows.append(row)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {output_path}")


if __name__ == "__main__":
    main()
