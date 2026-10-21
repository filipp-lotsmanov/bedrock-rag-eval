import importlib.util
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING

import boto3
import pytest
from moto import mock_aws

from ingest.chunk import DEFAULT_CONFIG, Chunk, ChunkingConfig
from ingest.emit import build_objects

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

_SPEC = importlib.util.spec_from_file_location(
    "sync_kb", Path(__file__).resolve().parents[1] / "scripts" / "sync_kb.py"
)
assert _SPEC is not None and _SPEC.loader is not None
sync_kb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(sync_kb)

RAW, PROCESSED = "raw-bucket", "processed-bucket"
CHUNKS = [Chunk(0, "First chunk.", 1, 1), Chunk(1, "Second chunk.", 1, 2)]


@pytest.fixture
def s3(monkeypatch: pytest.MonkeyPatch) -> Iterator["S3Client"]:
    for name, value in {
        "AWS_ACCESS_KEY_ID": "testing",
        "AWS_SECRET_ACCESS_KEY": "testing",
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "eu-central-1",
    }.items():
        monkeypatch.setenv(name, value)
    with mock_aws():
        client = boto3.client("s3")
        for bucket in (RAW, PROCESSED):
            client.create_bucket(
                Bucket=bucket, CreateBucketConfiguration={"LocationConstraint": "eu-central-1"}
            )
        yield client


def upload(s3: "S3Client", key: str) -> str:
    return s3.put_object(Bucket=RAW, Key=key, Body=b"%PDF-" + key.encode())["ETag"]


def ingest(s3: "S3Client", doc_id: str, etag: str, config: ChunkingConfig = DEFAULT_CONFIG) -> None:
    objects, manifest = build_objects(doc_id, "10.1/x", etag, CHUNKS, config)
    for obj in [*objects, manifest]:
        s3.put_object(Bucket=PROCESSED, Key=obj.key, Body=obj.body)


def test_current_documents_are_ready(s3: "S3Client") -> None:
    ingest(s3, "PMC1", upload(s3, "PMC1.pdf"))
    state = sync_kb.check_ready(s3, RAW, PROCESSED)
    assert state.problems == [] and set(state.docs) == {"PMC1"}


def test_chunks_from_another_config_are_not_ready(s3: "S3Client") -> None:
    old = ChunkingConfig(target_chars=999, overlap_sentences=0, max_chars=2000)
    ingest(s3, "PMC1", upload(s3, "PMC1.pdf"), old)
    state = sync_kb.check_ready(s3, RAW, PROCESSED)
    assert state.docs == {}
    assert len(state.problems) == 1 and "upload_corpus.py --force" in state.problems[0]


def test_a_pdf_not_named_by_doc_id_is_reported_not_raised(s3: "S3Client") -> None:
    ingest(s3, "PMC1", upload(s3, "PMC1.pdf"))
    upload(s3, "notes/reading.pdf")
    state = sync_kb.check_ready(s3, RAW, PROCESSED)
    assert set(state.docs) == {"PMC1"}
    assert state.problems == ["notes/reading.pdf: not named <doc_id>.pdf, so it is never ingested"]


def test_two_keys_with_one_doc_id_are_reported(s3: "S3Client") -> None:
    ingest(s3, "PMC1", upload(s3, "a/PMC1.pdf"))
    upload(s3, "b/PMC1.pdf")
    state = sync_kb.check_ready(s3, RAW, PROCESSED)
    assert state.docs == {}
    assert state.problems == ["PMC1: uploaded as a/PMC1.pdf, b/PMC1.pdf, which share chunks/PMC1/"]


def test_orphan_chunks_and_an_empty_bucket_are_reported(s3: "S3Client") -> None:
    ingest(s3, "PMC2", '"gone"')
    state = sync_kb.check_ready(s3, RAW, PROCESSED)
    assert state.problems == [f"no PDFs in {RAW}", "PMC2: chunks exist but its PDF does not"]
