"""Lambda entry point: one uploaded PDF in, its chunks, sidecars and manifest out.

Write order per document: delete the old manifest, delete the old chunks, write the new
chunks, write the new manifest. A manifest therefore only exists while a complete,
current chunk set is in place, which is what sync_kb.py relies on.

Each invocation works on one version of the PDF, the one its event names. When a newer upload
replaces it while the invocation runs, the invocation stops before deleting anything or before
writing its manifest, and leaves the document to the newer upload's invocation. A small window
remains: if the newer invocation finishes completely while this one is writing chunks, the two
chunk sets can mix under the newer manifest. Uploads of the same PDF seconds apart are needed
for that; upload_corpus.py --force repairs it.
"""

import functools
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.parse import unquote_plus

import boto3
from botocore.exceptions import ClientError

from ingest.chunk import DEFAULT_CONFIG, Chunk, chunk
from ingest.clean import clean_document
from ingest.emit import S3Object, build_objects, chunk_prefix, manifest_key, normalize_etag
from ingest.extract import extract_pages

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_s3.type_defs import ObjectIdentifierTypeDef

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

DELETE_BATCH = 1000


class IngestSummary(TypedDict):
    doc_id: str
    chunks: int
    deleted: int
    superseded: bool


@functools.cache
def _s3() -> "S3Client":
    return boto3.client("s3")


def doc_id_from_key(key: str) -> str:
    """PMC12756163.pdf (in any folder) -> PMC12756163; anything else is rejected."""
    path = PurePosixPath(key)
    if path.suffix != ".pdf":
        raise ValueError(f"{key!r} is not a .pdf key")
    chunk_prefix(path.stem)
    return path.stem


def pdf_to_chunks(pdf: bytes) -> list[Chunk]:
    return chunk(clean_document(extract_pages(pdf)), DEFAULT_CONFIG)


def delete_prefix(s3: "S3Client", bucket: str, prefix: str) -> int:
    """Delete every object under prefix; return how many were deleted."""
    keys = [
        item["Key"]
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
        for item in page.get("Contents", [])
    ]
    for i in range(0, len(keys), DELETE_BATCH):
        batch: list[ObjectIdentifierTypeDef] = [{"Key": key} for key in keys[i : i + DELETE_BATCH]]
        response = s3.delete_objects(Bucket=bucket, Delete={"Objects": batch, "Quiet": True})
        if response.get("Errors"):
            raise RuntimeError(f"failed to delete under {prefix!r}: {response['Errors']}")
    return len(keys)


def put_all(s3: "S3Client", bucket: str, objects: Iterable[S3Object]) -> None:
    for obj in objects:
        s3.put_object(Bucket=bucket, Key=obj.key, Body=obj.body, ContentType=obj.content_type)


def _current_etag(s3: "S3Client", bucket: str, key: str) -> str | None:
    """The key's current ETag, or None once it is deleted. Without s3:ListBucket on the raw
    bucket, S3 answers 403 rather than 404 for a deleted key; get_object has just succeeded,
    so a 403 here means the key is gone, not missing permissions."""
    try:
        return normalize_etag(s3.head_object(Bucket=bucket, Key=key)["ETag"])
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"403", "404", "NoSuchKey", "NotFound"}:
            return None
        raise


def ingest_pdf(
    s3: "S3Client", bucket: str, key: str, processed_bucket: str, etag: str | None = None
) -> IngestSummary:
    """Ingest the version of key whose ETag is etag (from the event), or the current version."""
    doc_id = doc_id_from_key(key)

    def superseded(deleted: int = 0, chunks: int = 0) -> IngestSummary:
        summary = IngestSummary(doc_id=doc_id, chunks=chunks, deleted=deleted, superseded=True)
        logger.warning("%s was replaced by a newer upload; stopped: %s", key, summary)
        return summary

    try:
        response = (
            s3.get_object(Bucket=bucket, Key=key, IfMatch=etag)
            if etag
            else s3.get_object(Bucket=bucket, Key=key)
        )
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") == "PreconditionFailed":
            return superseded()
        raise
    version = normalize_etag(response["ETag"])
    doi = response["Metadata"].get("doi")
    if not doi:
        raise ValueError(f"{key!r} has no x-amz-meta-doi metadata")
    objects, manifest = build_objects(
        doc_id,
        doi,
        response["ETag"],
        pdf_to_chunks(response["Body"].read()),
        DEFAULT_CONFIG,
    )

    if _current_etag(s3, bucket, key) != version:
        return superseded()
    s3.delete_object(Bucket=processed_bucket, Key=manifest_key(doc_id))
    deleted = delete_prefix(s3, processed_bucket, chunk_prefix(doc_id))
    put_all(s3, processed_bucket, objects)
    if _current_etag(s3, bucket, key) != version:
        return superseded(deleted, len(objects) // 2)
    put_all(s3, processed_bucket, [manifest])

    summary = IngestSummary(
        doc_id=doc_id, chunks=len(objects) // 2, deleted=deleted, superseded=False
    )
    logger.info("ingested %s", summary)
    return summary


def main(event: Mapping[str, Any], _context: object) -> dict[str, list[IngestSummary]]:
    processed_bucket = os.environ["PROCESSED_BUCKET"]
    results = [
        ingest_pdf(
            _s3(),
            record["s3"]["bucket"]["name"],
            unquote_plus(record["s3"]["object"]["key"]),
            processed_bucket,
            record["s3"]["object"].get("eTag"),
        )
        for record in event["Records"]
    ]
    return {"ingested": results}
