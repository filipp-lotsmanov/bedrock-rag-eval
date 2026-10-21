"""S3 objects for one document: chunk texts, their metadata sidecars, and the manifest.

Layout:
    chunks/<doc_id>/<index>.txt                 chunk text, embedded by the Knowledge Base
    chunks/<doc_id>/<index>.txt.metadata.json   sidecar, read by the Knowledge Base
    manifests/<doc_id>.json                     completion record, outside the ingested prefix

The handler writes the chunk objects first and the manifest last, so a manifest only
exists once every chunk of that document is in place.
"""

import functools
import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path

from ingest.chunk import Chunk, ChunkingConfig

CHUNK_PREFIX = "chunks"
MANIFEST_PREFIX = "manifests"
INDEX_WIDTH = 4
_DOC_ID = re.compile(r"PMC\d+")
# Chunk text depends on more than ChunkingConfig: on the extraction, cleanup and chunking code
# and on the parser and sentence splitter. config_id covers all of them, so any change to chunk
# output yields a new id. An edit that changes no output (a comment) does too; re-ingesting
# after one is harmless.
_STRATEGY_FILES = ("extract.py", "clean.py", "chunk.py")
_STRATEGY_PACKAGES = ("pypdf", "pysbd")


@dataclass(frozen=True)
class S3Object:
    key: str
    body: bytes
    content_type: str


@functools.cache
def strategy_fingerprint() -> dict[str, str]:
    """Hashes of the code that produces chunks, and the versions of the packages it uses."""
    here = Path(__file__).parent
    code = {
        name: hashlib.sha256((here / name).read_bytes()).hexdigest() for name in _STRATEGY_FILES
    }
    return code | {name: metadata.version(name) for name in _STRATEGY_PACKAGES}


def config_id(config: ChunkingConfig) -> str:
    """A short, stable hash of the chunking strategy, recorded with every chunk: the config,
    the code that applies it, and the package versions."""
    payload = json.dumps(
        {"config": asdict(config), "strategy": strategy_fingerprint()}, sort_keys=True
    ).encode()
    return hashlib.sha256(payload).hexdigest()[:12]


def normalize_etag(etag: str) -> str:
    """S3 returns ETags quoted in API responses and unquoted in event records."""
    return etag.strip('"')


def chunk_prefix(doc_id: str) -> str:
    return f"{CHUNK_PREFIX}/{_checked(doc_id)}/"


def manifest_key(doc_id: str) -> str:
    return f"{MANIFEST_PREFIX}/{_checked(doc_id)}.json"


def _checked(doc_id: str) -> str:
    if not _DOC_ID.fullmatch(doc_id):
        raise ValueError(f"doc_id must look like PMC12345, got {doc_id!r}")
    return doc_id


def _json(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False).encode("utf-8")


def build_objects(
    doc_id: str,
    doi: str,
    pdf_etag: str,
    chunks: Sequence[Chunk],
    config: ChunkingConfig,
) -> tuple[list[S3Object], S3Object]:
    """Return (chunk objects, manifest). Write the chunk objects first, the manifest last."""
    if not chunks:
        raise ValueError(f"{doc_id}: no chunks to emit")
    if len(chunks) >= 10**INDEX_WIDTH:
        raise ValueError(f"{doc_id}: {len(chunks)} chunks do not fit {INDEX_WIDTH}-digit keys")

    prefix = chunk_prefix(doc_id)
    cid = config_id(config)
    objects = []
    for c in chunks:
        key = f"{prefix}{c.index:0{INDEX_WIDTH}d}.txt"
        metadata: dict[str, object] = {
            "doc_id": doc_id,
            "doi": doi,
            "page_start": c.page_start,
            "page_end": c.page_end,
            "chunk_index": c.index,
            "config_id": cid,
        }
        objects.append(S3Object(key, c.text.encode("utf-8"), "text/plain; charset=utf-8"))
        objects.append(
            S3Object(
                f"{key}.metadata.json",
                _json({"metadataAttributes": metadata}),
                "application/json",
            )
        )

    manifest = S3Object(
        manifest_key(doc_id),
        _json(
            {
                "doc_id": doc_id,
                "pdf_etag": normalize_etag(pdf_etag),
                "chunk_count": len(chunks),
                "config_id": cid,
            }
        ),
        "application/json",
    )
    return objects, manifest
