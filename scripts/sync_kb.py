"""Sync the Knowledge Base once every uploaded PDF has a complete, current chunk set.

Readiness, checked per PDF in the raw bucket:
    - the key is named <doc_id>.pdf, and no two keys share a doc_id (they would share chunks)
    - manifests/<doc_id>.json exists and its pdf_etag matches the PDF's current ETag
    - chunks/<doc_id>/ holds exactly chunk_count texts and chunk_count sidecars
    - every manifest's config_id is the one the current ingest code produces, so a pipeline
      change that was not re-ingested is caught
    - no chunks/<doc_id>/ folder exists without its PDF (orphans would be embedded)

Once ready, one ingestion job runs to completion, and its ID, statistics and the index
state it produced are written to build/last_sync.json for eval provenance.

Usage: uv run --group ingest python scripts/sync_kb.py --raw-bucket <raw> --processed-bucket <processed>
"""

import argparse
import json
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

from ingest.chunk import DEFAULT_CONFIG
from ingest.emit import CHUNK_PREFIX, chunk_prefix, config_id, manifest_key, normalize_etag
from ingest.handler import doc_id_from_key

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agent import AgentsforBedrockClient
    from mypy_boto3_s3 import S3Client

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "build" / "last_sync.json"
FINISHED = {"COMPLETE", "FAILED", "STOPPED"}


@dataclass
class Readiness:
    docs: dict[str, dict[str, Any]] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def _keys(s3: "S3Client", bucket: str, prefix: str) -> list[tuple[str, str]]:
    """(key, ETag) for every object under prefix."""
    pages = s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    return [(item["Key"], item["ETag"]) for page in pages for item in page.get("Contents", [])]


def _chunk_folders(s3: "S3Client", bucket: str) -> set[str]:
    pages = s3.get_paginator("list_objects_v2").paginate(
        Bucket=bucket, Prefix=f"{CHUNK_PREFIX}/", Delimiter="/"
    )
    return {p["Prefix"].split("/")[1] for page in pages for p in page.get("CommonPrefixes", [])}


def check_ready(s3: "S3Client", raw_bucket: str, processed_bucket: str) -> Readiness:
    state = Readiness()
    expected_config = config_id(DEFAULT_CONFIG)
    pdfs: dict[str, str] = {}
    keys_by_doc: dict[str, list[str]] = {}
    for key, etag in _keys(s3, raw_bucket, ""):
        if not key.endswith(".pdf"):
            continue
        try:
            doc_id = doc_id_from_key(key)
        except ValueError:
            state.problems.append(f"{key}: not named <doc_id>.pdf, so it is never ingested")
            continue
        pdfs[doc_id] = normalize_etag(etag)
        keys_by_doc.setdefault(doc_id, []).append(key)
    for doc_id, keys in sorted(keys_by_doc.items()):
        if len(keys) > 1:
            state.problems.append(
                f"{doc_id}: uploaded as {', '.join(sorted(keys))}, which share chunks/{doc_id}/"
            )
            del pdfs[doc_id]
    if not pdfs and not keys_by_doc:
        state.problems.append(f"no PDFs in {raw_bucket}")
    for doc_id, etag in sorted(pdfs.items()):
        try:
            body = s3.get_object(Bucket=processed_bucket, Key=manifest_key(doc_id))["Body"].read()
        except s3.exceptions.NoSuchKey:
            state.problems.append(f"{doc_id}: no manifest yet")
            continue
        manifest = json.loads(body)
        if manifest["pdf_etag"] != etag:
            state.problems.append(f"{doc_id}: manifest is for an older upload")
            continue
        keys = [key for key, _ in _keys(s3, processed_bucket, chunk_prefix(doc_id))]
        texts = sum(key.endswith(".txt") for key in keys)
        sidecars = sum(key.endswith(".txt.metadata.json") for key in keys)
        if texts != manifest["chunk_count"] or sidecars != manifest["chunk_count"]:
            state.problems.append(
                f"{doc_id}: {texts} texts and {sidecars} sidecars, manifest says {manifest['chunk_count']}"
            )
            continue
        if manifest["config_id"] != expected_config:
            state.problems.append(
                f"{doc_id}: chunked with config {manifest['config_id']}, the current ingest code "
                f"gives {expected_config}; deploy it and re-upload with upload_corpus.py --force"
            )
            continue
        state.docs[doc_id] = manifest
    for orphan in sorted(_chunk_folders(s3, processed_bucket) - set(keys_by_doc)):
        state.problems.append(f"{orphan}: chunks exist but its PDF does not")
    return state


def wait_until_ready(
    check: Callable[[], Readiness],
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Readiness:
    deadline = clock() + timeout
    while True:
        state = check()
        if not state.problems:
            return state
        if clock() >= deadline:
            sys.exit("not ready:\n  " + "\n  ".join(state.problems))
        print(f"waiting: {len(state.problems)} problem(s), first: {state.problems[0]}")
        sleep(interval)


def run_ingestion(
    agent: "AgentsforBedrockClient",
    kb_id: str,
    ds_id: str,
    timeout: float,
    interval: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    job_id = agent.start_ingestion_job(knowledgeBaseId=kb_id, dataSourceId=ds_id)["ingestionJob"][
        "ingestionJobId"
    ]
    print(f"started ingestion job {job_id}")
    deadline = clock() + timeout
    while True:
        job = agent.get_ingestion_job(
            knowledgeBaseId=kb_id, dataSourceId=ds_id, ingestionJobId=job_id
        )
        status = job["ingestionJob"]["status"]
        if status in FINISHED:
            return dict(job["ingestionJob"])
        if clock() >= deadline:
            sys.exit(f"ingestion job {job_id} still {status} after {timeout:.0f}s")
        print(f"  {status}")
        sleep(interval)


def terraform_outputs() -> dict[str, str]:
    result = subprocess.run(
        ["terraform", f"-chdir={ROOT / 'infra'}", "output", "-json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        sys.exit("could not read terraform outputs; pass --kb-id and --ds-id instead")
    return {name: output["value"] for name, output in json.loads(result.stdout).items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw-bucket", required=True)
    parser.add_argument("--processed-bucket", required=True)
    parser.add_argument("--kb-id", help="default: terraform output knowledge_base_id")
    parser.add_argument("--ds-id", help="default: terraform output data_source_id")
    parser.add_argument("--wait", type=float, default=600, help="seconds to wait for manifests")
    parser.add_argument(
        "--job-timeout", type=float, default=1800, help="seconds to wait for the job"
    )
    args = parser.parse_args()

    outputs = terraform_outputs() if not (args.kb_id and args.ds_id) else {}
    kb_id = args.kb_id or outputs["knowledge_base_id"]
    ds_id = args.ds_id or outputs["data_source_id"]

    s3 = boto3.client("s3")
    state = wait_until_ready(
        lambda: check_ready(s3, args.raw_bucket, args.processed_bucket), args.wait, 10
    )
    print(
        f"ready: {len(state.docs)} documents, {sum(m['chunk_count'] for m in state.docs.values())} chunks"
    )

    job = run_ingestion(boto3.client("bedrock-agent"), kb_id, ds_id, args.job_timeout, 15)
    record = {
        "recorded_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "knowledge_base_id": kb_id,
        "data_source_id": ds_id,
        "ingestion_job_id": job["ingestionJobId"],
        "status": job["status"],
        "statistics": job.get("statistics", {}),
        "failure_reasons": job.get("failureReasons", []),
        "config_id": next(iter({m["config_id"] for m in state.docs.values()})),
        "documents": {
            doc_id: {"pdf_etag": m["pdf_etag"], "chunk_count": m["chunk_count"]}
            for doc_id, m in sorted(state.docs.items())
        },
    }
    RECORD.parent.mkdir(exist_ok=True)
    RECORD.write_text(json.dumps(record, indent=2, default=str) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {k: record[k] for k in ("status", "statistics", "failure_reasons")},
            indent=2,
            default=str,
        )
    )
    print(f"recorded in {RECORD}")
    if job["status"] != "COMPLETE" or record["statistics"].get("numberOfDocumentsFailed", 0):
        sys.exit(1)


if __name__ == "__main__":
    main()
