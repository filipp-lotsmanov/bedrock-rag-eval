import json

import pytest

from ingest.chunk import Chunk, ChunkingConfig
from ingest.emit import (
    S3Object,
    build_objects,
    chunk_prefix,
    config_id,
    manifest_key,
    normalize_etag,
)

CONFIG = ChunkingConfig(target_chars=1500, overlap_sentences=1, max_chars=3000)
CHUNKS = [
    Chunk(0, "DSV increased \u03b1-syn levels.", 1, 1),
    Chunk(1, "This increase spans a page break.", 1, 2),
]


def build(chunks: list[Chunk] = CHUNKS) -> tuple[list[S3Object], S3Object]:
    return build_objects("PMC12756163", "10.3389/fnins.2025.1672793", '"abc123"', chunks, CONFIG)


def test_config_id_is_stable_and_config_specific() -> None:
    same = ChunkingConfig(target_chars=1500, overlap_sentences=1, max_chars=3000)
    other = ChunkingConfig(target_chars=1500, overlap_sentences=0, max_chars=3000)
    assert config_id(CONFIG) == config_id(same)
    assert config_id(CONFIG) != config_id(other)
    assert len(config_id(CONFIG)) == 12
    int(config_id(CONFIG), 16)


def test_normalize_etag_strips_quotes() -> None:
    assert normalize_etag('"abc123"') == "abc123"
    assert normalize_etag("abc123") == "abc123"


def test_keys_pair_each_chunk_with_its_sidecar_in_order() -> None:
    objects, _ = build()
    assert [o.key for o in objects] == [
        "chunks/PMC12756163/0000.txt",
        "chunks/PMC12756163/0000.txt.metadata.json",
        "chunks/PMC12756163/0001.txt",
        "chunks/PMC12756163/0001.txt.metadata.json",
    ]


def test_sidecar_matches_knowledge_base_format() -> None:
    objects, _ = build()
    sidecar = json.loads(objects[3].body)
    assert sidecar == {
        "metadataAttributes": {
            "doc_id": "PMC12756163",
            "doi": "10.3389/fnins.2025.1672793",
            "page_start": 1,
            "page_end": 2,
            "chunk_index": 1,
            "config_id": config_id(CONFIG),
        }
    }
    assert objects[3].content_type == "application/json"


def test_manifest_is_outside_the_ingested_prefix() -> None:
    objects, manifest = build()
    assert manifest.key == "manifests/PMC12756163.json"
    assert not manifest.key.startswith("chunks/")
    assert all(o.key.startswith(chunk_prefix("PMC12756163")) for o in objects)
    assert json.loads(manifest.body) == {
        "doc_id": "PMC12756163",
        "pdf_etag": "abc123",
        "chunk_count": 2,
        "config_id": config_id(CONFIG),
    }


@pytest.mark.parametrize("doc_id", ["", "12756163", "PMC12a", "PMC1/../x", "../PMC1", "pmc123"])
def test_invalid_doc_ids_are_rejected(doc_id: str) -> None:
    with pytest.raises(ValueError):
        manifest_key(doc_id)
    with pytest.raises(ValueError):
        chunk_prefix(doc_id)


def test_empty_document_is_rejected() -> None:
    with pytest.raises(ValueError):
        build([])
