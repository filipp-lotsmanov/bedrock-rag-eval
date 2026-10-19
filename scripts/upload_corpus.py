"""Upload the corpus PDFs to the raw bucket, each with its DOI as x-amz-meta-doi.

Every upload triggers the ingest Lambda. Before uploading, each PDF's SHA-256 is checked
against corpus/manifest.csv, and its DOI is read from the matching JATS XML. PDFs already
in the bucket with the same bytes and DOI are skipped, so re-running is safe and does not
re-ingest anything; --force uploads them anyway, to re-ingest after a pipeline change. Uploads are single-part, so the S3 ETag is the file's MD5.

Usage: uv run python scripts/upload_corpus.py --bucket <raw-bucket> [--dry-run] [--force]
"""

import argparse
import csv
import hashlib
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
from botocore.exceptions import ClientError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "corpus"


def read_manifest(path: Path) -> dict[str, str]:
    """PMCID -> expected PDF SHA-256."""
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows or not {"pmcid", "pdf_sha256"} <= set(rows[0]):
        sys.exit(f"{path} needs 'pmcid' and 'pdf_sha256' columns")
    return {row["pmcid"]: row["pdf_sha256"] for row in rows}


def read_doi(xml_path: Path) -> str:
    """The article DOI from a JATS file's article-meta."""
    element = ET.parse(xml_path).find(".//article-meta/article-id[@pub-id-type='doi']")
    doi = (element.text or "").strip() if element is not None else ""
    if not doi or not doi.isascii():
        sys.exit(f"{xml_path}: no usable DOI in article-meta")
    return doi


def remote_state(s3: "S3Client", bucket: str, key: str) -> tuple[str, str] | None:
    """(ETag without quotes, DOI metadata) of an existing object, or None if absent."""
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {
            "404",
            "NoSuchKey",
            "NotFound",
        }:
            return None
        raise
    return head["ETag"].strip('"'), head["Metadata"].get("doi", "")


def upload_corpus(
    s3: "S3Client", bucket: str, corpus: Path, dry_run: bool, force: bool = False
) -> None:
    expected = read_manifest(corpus / "manifest.csv")
    uploaded = skipped = 0
    for pmcid, sha256 in sorted(expected.items()):
        pdf_path, xml_path = (
            corpus / "pdf" / f"{pmcid}.pdf",
            corpus / "xml" / f"{pmcid}.xml",
        )
        for path in (pdf_path, xml_path):
            if not path.is_file():
                sys.exit(f"missing {path}")
        pdf = pdf_path.read_bytes()
        if hashlib.sha256(pdf).hexdigest() != sha256:
            sys.exit(f"{pdf_path}: SHA-256 does not match manifest.csv")
        doi, key = read_doi(xml_path), pdf_path.name

        if not force and remote_state(s3, bucket, key) == (
            hashlib.md5(pdf, usedforsecurity=False).hexdigest(),
            doi,
        ):
            print(f"skip    {key}  unchanged  doi={doi}")
            skipped += 1
            continue
        print(f"{'would upload' if dry_run else 'upload'}  {key}  doi={doi}")
        if not dry_run:
            s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=pdf,
                ContentType="application/pdf",
                Metadata={"doi": doi},
            )
        uploaded += 1
    action = "to upload" if dry_run else "uploaded"
    print(f"{uploaded} {action}, {skipped} unchanged, {len(expected)} in manifest")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket", required=True, help="the raw bucket")
    parser.add_argument(
        "--dry-run", action="store_true", help="show what would be uploaded"
    )
    parser.add_argument(
        "--force", action="store_true", help="upload unchanged PDFs too, to re-ingest"
    )
    args = parser.parse_args()
    upload_corpus(boto3.client("s3"), args.bucket, CORPUS, args.dry_run, args.force)