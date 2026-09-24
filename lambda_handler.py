"""AWS Lambda entry point: render the parking map and upload it to S3.

Fetches today's parking lots, renders the PDF (and PNG preview) with the
basemap and poster labels baked into the image, and uploads both to
the root of s3://S3_BUCKET. PARKING_URL comes from the image (set at build
time from the Makefile); S3_BUCKET comes from the function's
environment, or from local.env via the Makefile for `make run-aws`.
"""

import json
import os
import urllib.request
from pathlib import Path
from typing import Any

import boto3

from main import render

# Baked into the image next to this file; see the Dockerfile.
HERE: Path = Path(__file__).resolve().parent
BASEMAP: Path = HERE / "data" / "basemap.geojson"
LABELS: Path = HERE / "data" / "poster_labels.csv"

# Lambda can only write to /tmp.
WORK: Path = Path("/tmp")
PDF_NAME: str = "student-parking.pdf"

# Same check as the Makefile's parking target.
PARKING_FIELDS: set[str] = {
    "NAME",
    "LOT",
    "PERMIT_SPACES",
    "ENFORCEMENT",
    "PERMITS_ACCEPTED",
}
FETCH_TIMEOUT: float = 30.0


def fetch_parking(url: str, path: Path) -> int:
    """Download parking lots to path, failing if none or fields are missing.

    Returns the number of lots.
    """
    response: Any
    with urllib.request.urlopen(url, timeout=FETCH_TIMEOUT) as response:
        body: bytes = response.read()
    features: list[dict[str, Any]] = json.loads(body).get("features", [])
    if not features:
        raise ValueError("parking: no features")
    feature: dict[str, Any]
    for feature in features:
        missing: set[str] = PARKING_FIELDS - feature.get("properties", {}).keys()
        if missing:
            raise ValueError(f"parking: missing fields {sorted(missing)}")
    path.write_bytes(body)
    return len(features)


def upload(path: Path, bucket: str, key: str, content_type: str) -> None:
    """Upload a file to S3."""
    boto3.client("s3").upload_file(
        str(path), bucket, key, ExtraArgs={"ContentType": content_type}
    )
    print(f"uploaded s3://{bucket}/{key}")


def lambda_handler(event: Any, context: Any) -> None:
    """Fetch parking, render the map, and upload the PDF and PNG to S3."""
    bucket: str = os.environ["S3_BUCKET"]
    parking: Path = WORK / "parking.geojson"
    count: int = fetch_parking(os.environ["PARKING_URL"], parking)
    print(f"parking: {count} lots")

    pdf: Path = WORK / PDF_NAME
    render(parking, BASEMAP, LABELS, pdf)
    upload(pdf, bucket, pdf.name, "application/pdf")
    # render() writes the PNG preview next to the PDF with the same name.
    png: Path = pdf.with_suffix(".png")
    upload(png, bucket, png.name, "image/png")


if __name__ == "__main__":
    lambda_handler(None, None)
