"""Shared helpers for extraction scripts: file discovery across local folders or GCS buckets."""

from pathlib import Path
from typing import List

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}
VIDEO_EXTENSIONS = {".mp4", ".mov"}


def is_gcs_path(path: str) -> bool:
    return path.startswith("gs://")


def media_type_for(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext in VIDEO_EXTENSIONS:
        return "video"
    if ext in IMAGE_EXTENSIONS:
        return "photo"
    return "unknown"


def list_media_files(folder: str) -> List[str]:
    """Returns a sorted list of file identifiers: local paths, or gs:// URIs."""
    if is_gcs_path(folder):
        from google.cloud import storage

        bucket_name, _, prefix = folder[len("gs://"):].partition("/")
        client = storage.Client()
        blobs = client.list_blobs(bucket_name, prefix=prefix)
        return sorted(
            f"gs://{bucket_name}/{blob.name}"
            for blob in blobs
            if not blob.name.endswith("/")
        )
    return sorted(str(f) for f in Path(folder).iterdir() if f.is_file())


def read_bytes(path: str) -> bytes:
    if is_gcs_path(path):
        from google.cloud import storage

        bucket_name, _, blob_name = path[len("gs://"):].partition("/")
        client = storage.Client()
        return client.bucket(bucket_name).blob(blob_name).download_as_bytes()
    return Path(path).read_bytes()


def basename(path: str) -> str:
    return path.rsplit("/", 1)[-1] if is_gcs_path(path) else Path(path).name
