import json
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import pytest
from moto import mock_aws

from ingest import handler
from ingest.chunk import Chunk

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

RAW, PROCESSED = "raw-bucket", "processed-bucket"
DOI = "10.3389/fnins.2025.1672793"
CORPUS = Path(__file__).resolve().parents[1] / "corpus" / "pdf"


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator["S3Client"]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "eu-central-1",
        "PROCESSED_BUCKET": PROCESSED,
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        client = boto3.client("s3")
        for bucket in (RAW, PROCESSED):
            client.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": "eu-central-1"},
            )
        handler._s3.cache_clear()
        yield client
    handler._s3.cache_clear()


@pytest.fixture
def two_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [Chunk(0, "First chunk.", 1, 1), Chunk(1, "Second chunk.", 1, 2)]
    monkeypatch.setattr(handler, "pdf_to_chunks", lambda pdf: chunks)


def upload(
    s3: "S3Client", key: str = "PMC1.pdf", doi: str | None = DOI, body: bytes = b"%PDF-"
) -> None:
    metadata = {"doi": doi} if doi else {}
    s3.put_object(Bucket=RAW, Key=key, Body=body, Metadata=metadata)


def keys(s3: "S3Client", prefix: str = "") -> list[str]:
    response = s3.list_objects_v2(Bucket=PROCESSED, Prefix=prefix)
    return sorted(item["Key"] for item in response.get("Contents", []))


def event(*object_keys: str, etag: str | None = None) -> dict[str, Any]:
    obj = {} if etag is None else {"eTag": etag}
    return {
        "Records": [
            {"s3": {"bucket": {"name": RAW}, "object": {"key": key, **obj}}} for key in object_keys
        ]
    }


@pytest.mark.parametrize(
    ("key", "expected"), [("PMC1.pdf", "PMC1"), ("raw/PMC12756163.pdf", "PMC12756163")]
)
def test_doc_id_from_key(key: str, expected: str) -> None:
    assert handler.doc_id_from_key(key) == expected


@pytest.mark.parametrize("key", ["PMC1.PDF", "PMC1.txt", "notes.pdf", "../PMC1x.pdf"])
def test_doc_id_from_key_rejects(key: str) -> None:
    with pytest.raises(ValueError):
        handler.doc_id_from_key(key)


@pytest.mark.usefixtures("two_chunks")
def test_writes_chunks_sidecars_and_manifest(s3: "S3Client") -> None:
    upload(s3)
    result = handler.main(event("PMC1.pdf"), None)

    assert result == {
        "ingested": [{"doc_id": "PMC1", "chunks": 2, "deleted": 0, "superseded": False}]
    }
    assert keys(s3) == [
        "chunks/PMC1/0000.txt",
        "chunks/PMC1/0000.txt.metadata.json",
        "chunks/PMC1/0001.txt",
        "chunks/PMC1/0001.txt.metadata.json",
        "manifests/PMC1.json",
    ]
    manifest = json.loads(s3.get_object(Bucket=PROCESSED, Key="manifests/PMC1.json")["Body"].read())
    pdf_etag = s3.head_object(Bucket=RAW, Key="PMC1.pdf")["ETag"].strip('"')
    assert manifest["pdf_etag"] == pdf_etag
    assert manifest["chunk_count"] == 2
    sidecar = json.loads(
        s3.get_object(Bucket=PROCESSED, Key="chunks/PMC1/0001.txt.metadata.json")["Body"].read()
    )
    assert sidecar["metadataAttributes"]["doi"] == DOI


@pytest.mark.usefixtures("two_chunks")
def test_reingest_removes_stale_chunks_but_not_other_documents(s3: "S3Client") -> None:
    for i in range(5):
        s3.put_object(Bucket=PROCESSED, Key=f"chunks/PMC1/{i:04d}.txt", Body=b"old")
    s3.put_object(Bucket=PROCESSED, Key="chunks/PMC12/0000.txt", Body=b"other document")
    s3.put_object(Bucket=PROCESSED, Key="manifests/PMC1.json", Body=b"{}")
    upload(s3)

    result = handler.main(event("PMC1.pdf"), None)

    assert result["ingested"] == [
        {"doc_id": "PMC1", "chunks": 2, "deleted": 5, "superseded": False}
    ]
    assert keys(s3, "chunks/PMC1/") == [
        "chunks/PMC1/0000.txt",
        "chunks/PMC1/0000.txt.metadata.json",
        "chunks/PMC1/0001.txt",
        "chunks/PMC1/0001.txt.metadata.json",
    ]
    assert keys(s3, "chunks/PMC12/") == ["chunks/PMC12/0000.txt"]
    assert (
        s3.get_object(Bucket=PROCESSED, Key="chunks/PMC1/0000.txt")["Body"].read()
        == b"First chunk."
    )


@pytest.mark.usefixtures("two_chunks")
def test_manifest_is_written_last(s3: "S3Client", monkeypatch: pytest.MonkeyPatch) -> None:
    upload(s3)
    written: list[str] = []
    original = s3.put_object

    def spy(**kwargs: Any) -> Any:
        written.append(kwargs["Key"])
        return original(**kwargs)

    monkeypatch.setattr(s3, "put_object", spy)
    handler.ingest_pdf(s3, RAW, "PMC1.pdf", PROCESSED)
    assert written[-1] == "manifests/PMC1.json"
    assert len(written) == 5


@pytest.mark.usefixtures("two_chunks")
def test_event_for_a_replaced_version_changes_nothing(s3: "S3Client") -> None:
    upload(s3, body=b"%PDF- version 1")
    old_etag = s3.head_object(Bucket=RAW, Key="PMC1.pdf")["ETag"].strip('"')
    upload(s3, body=b"%PDF- version 2")
    s3.put_object(Bucket=PROCESSED, Key="manifests/PMC1.json", Body=b"{}")

    result = handler.main(event("PMC1.pdf", etag=old_etag), None)

    assert result["ingested"] == [{"doc_id": "PMC1", "chunks": 0, "deleted": 0, "superseded": True}]
    assert keys(s3) == ["manifests/PMC1.json"]


@pytest.mark.usefixtures("two_chunks")
def test_event_for_the_current_version_is_ingested(s3: "S3Client") -> None:
    upload(s3)
    etag = s3.head_object(Bucket=RAW, Key="PMC1.pdf")["ETag"].strip('"')
    result = handler.main(event("PMC1.pdf", etag=etag), None)
    assert result["ingested"][0]["superseded"] is False
    assert "manifests/PMC1.json" in keys(s3)


def test_upload_during_parsing_stops_before_deleting(
    s3: "S3Client", monkeypatch: pytest.MonkeyPatch
) -> None:
    upload(s3, body=b"%PDF- version 1")
    s3.put_object(Bucket=PROCESSED, Key="chunks/PMC1/0000.txt", Body=b"current")

    def parse_while_replaced(pdf: bytes) -> list[Chunk]:
        upload(s3, body=b"%PDF- version 2")
        return [Chunk(0, "Stale chunk.", 1, 1)]

    monkeypatch.setattr(handler, "pdf_to_chunks", parse_while_replaced)
    summary = handler.ingest_pdf(s3, RAW, "PMC1.pdf", PROCESSED)
    assert summary["superseded"] is True
    assert keys(s3) == ["chunks/PMC1/0000.txt"]


def test_upload_during_writing_withholds_the_manifest(
    s3: "S3Client", monkeypatch: pytest.MonkeyPatch
) -> None:
    upload(s3, body=b"%PDF- version 1")
    monkeypatch.setattr(handler, "pdf_to_chunks", lambda pdf: [Chunk(0, "v1 chunk.", 1, 1)])
    original = handler.put_all

    def put_then_replace(s3_: "S3Client", bucket: str, objects: Any) -> None:
        original(s3_, bucket, objects)
        upload(s3, body=b"%PDF- version 2")

    monkeypatch.setattr(handler, "put_all", put_then_replace)
    summary = handler.ingest_pdf(s3, RAW, "PMC1.pdf", PROCESSED)
    assert summary == {"doc_id": "PMC1", "chunks": 1, "deleted": 0, "superseded": True}
    assert "manifests/PMC1.json" not in keys(s3)


def test_deleted_pdf_counts_as_replaced(s3: "S3Client", monkeypatch: pytest.MonkeyPatch) -> None:
    upload(s3)

    def parse_while_deleted(pdf: bytes) -> list[Chunk]:
        s3.delete_object(Bucket=RAW, Key="PMC1.pdf")
        return [Chunk(0, "Chunk.", 1, 1)]

    monkeypatch.setattr(handler, "pdf_to_chunks", parse_while_deleted)
    assert handler.ingest_pdf(s3, RAW, "PMC1.pdf", PROCESSED)["superseded"] is True


@pytest.mark.usefixtures("two_chunks")
def test_missing_doi_fails_before_touching_existing_chunks(s3: "S3Client") -> None:
    s3.put_object(Bucket=PROCESSED, Key="chunks/PMC1/0000.txt", Body=b"old")
    s3.put_object(Bucket=PROCESSED, Key="manifests/PMC1.json", Body=b"{}")
    upload(s3, doi=None)

    with pytest.raises(ValueError, match="doi"):
        handler.main(event("PMC1.pdf"), None)
    assert keys(s3) == ["chunks/PMC1/0000.txt", "manifests/PMC1.json"]


@pytest.mark.usefixtures("two_chunks")
def test_event_keys_are_url_decoded(s3: "S3Client") -> None:
    upload(s3, key="new uploads/PMC1.pdf")
    result = handler.main(event("new+uploads/PMC1.pdf"), None)
    assert result["ingested"][0]["doc_id"] == "PMC1"


@pytest.mark.usefixtures("two_chunks")
def test_delete_prefix_handles_more_than_one_batch(
    s3: "S3Client", monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(handler, "DELETE_BATCH", 3)
    for i in range(7):
        s3.put_object(Bucket=PROCESSED, Key=f"chunks/PMC1/{i:04d}.txt", Body=b"old")
    assert handler.delete_prefix(s3, PROCESSED, "chunks/PMC1/") == 7
    assert keys(s3, "chunks/PMC1/") == []


corpus_pdfs = sorted(CORPUS.glob("*.pdf")) if CORPUS.is_dir() else []


@pytest.mark.skipif(not corpus_pdfs, reason="corpus PDFs are not in the repository")
def test_real_pdf_end_to_end(s3: "S3Client") -> None:
    pdf = corpus_pdfs[0]
    upload(s3, key=pdf.name, body=pdf.read_bytes())
    result = handler.main(event(pdf.name), None)

    count = result["ingested"][0]["chunks"]
    assert count > 0
    assert len(keys(s3, f"chunks/{pdf.stem}/")) == 2 * count
    assert keys(s3, "manifests/") == [f"manifests/{pdf.stem}.json"]
