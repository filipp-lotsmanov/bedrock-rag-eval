"""Lambda entry point: one uploaded PDF in, its chunks, sidecars and manifest out.

Write order per document: delete the old manifest, delete the old chunks, write the new
chunks, write the new manifest. A manifest therefore only exists while a complete,
current chunk set is in place, which is what sync_kb.py relies on.
"""

import functools
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any, TypedDict
from urllib.parse import unquote_plus

import boto3

from ingest.chunk import DEFAULT_CONFIG, Chunk, chunk
from ingest.clean import clean_document
from ingest.emit import S3Object, build_objects, chunk_prefix, manifest_key
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
        for page in s3.get_paginator("list_objects_v2").paginate(
            Bucket=bucket, Prefix=prefix
        )
        for item in page.get("Contents", [])
    ]
    for i in range(0, len(keys), DELETE_BATCH):
        batch: list[ObjectIdentifierTypeDef] = [
            {"Key": key} for key in keys[i : i + DELETE_BATCH]
        ]
        response = s3.delete_objects(
            Bucket=bucket, Delete={"Objects": batch, "Quiet": True}
        )
        if response.get("Errors"):
            raise RuntimeError(
                f"failed to delete under {prefix!r}: {response['Errors']}"
            )
    return len(keys)


def put_all(s3: "S3Client", bucket: str, objects: Iterable[S3Object]) -> None:
    for obj in objects:
        s3.put_object(
            Bucket=bucket, Key=obj.key, Body=obj.body, ContentType=obj.content_type
        )


def ingest_pdf(
    s3: "S3Client", bucket: str, key: str, processed_bucket: str
) -> IngestSummary:
    doc_id = doc_id_from_key(key)
    response = s3.get_object(Bucket=bucket, Key=key)
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

    s3.delete_object(Bucket=processed_bucket, Key=manifest_key(doc_id))
    deleted = delete_prefix(s3, processed_bucket, chunk_prefix(doc_id))
    put_all(s3, processed_bucket, objects)
    put_all(s3, processed_bucket, [manifest])

    summary = IngestSummary(doc_id=doc_id, chunks=len(objects) // 2, deleted=deleted)
    logger.info("ingested %s", summary)
    return summary


def main(event: Mapping[str, Any], context: object) -> dict[str, list[IngestSummary]]:
    processed_bucket = os.environ["PROCESSED_BUCKET"]
    results = [
        ingest_pdf(
            _s3(),
            record["s3"]["bucket"]["name"],
            unquote_plus(record["s3"]["object"]["key"]),
            processed_bucket,
        )
        for record in event["Records"]
    ]
    return {"ingested": results}