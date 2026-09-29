"""Build a paired PDF/XML corpus from the PMC Open Access Subset.

Selection runs through NCBI E-utilities; validation and download use the public
pmc-oa-opendata S3 bucket over unsigned HTTPS. No AWS credentials required.

Filters applied per article:
    is_pmc_openaccess == True
    is_retracted      == False
    is_historical_ocr == False   (excludes scanned 19th/20th century material)
    license_code      in {CC0, CC BY}

The latest available version of each article is used. manifest.csv is the
authoritative corpus listing: downstream code iterates it rather than globbing
the output directories, so stray files never enter an evaluation.

Usage:
    uv run python scripts/fetch_corpus.py --count 10
    uv run python scripts/fetch_corpus.py --count 300 --out corpus
"""

import argparse
import csv
import hashlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ESEARCH = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
BUCKET = "https://pmc-oa-opendata.s3.amazonaws.com"
ALLOWED_LICENSES = {"CC0", "CC BY"}
MAX_VERSION = 3
REQUEST_DELAY = 0.4
"""Pause between article lookups, to stay polite to the public bucket."""

USER_AGENT = {"User-Agent": "bedrock-rag-eval/0.1"}
NETWORK_ERRORS = (urllib.error.URLError, TimeoutError)
"""HTTPError subclasses URLError, so callers must catch HTTPError first."""


def get_json(url: str) -> dict[str, Any]:
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def search_pmcids(journal: str, mindate: str, maxdate: str, retmax: int) -> list[str]:
    term = f'"{journal}"[journal] AND "open access"[filter]'
    params = {
        "db": "pmc",
        "term": term,
        "retmax": str(retmax),
        "retmode": "json",
        "datetype": "pdat",
        "mindate": mindate,
        "maxdate": maxdate,
        "sort": "pub_date",
    }
    url = f"{ESEARCH}?{urllib.parse.urlencode(params)}"
    result = get_json(url)
    ids = result.get("esearchresult", {}).get("idlist", [])
    return [f"PMC{i}" for i in ids]


def fetch_metadata(pmcid: str) -> tuple[str, dict[str, Any]] | None:
    """Return (versioned_id, metadata) for the latest version that resolves.

    Network errors propagate so the caller can count them separately from
    articles that genuinely have no S3 prefix.
    """
    for version in range(MAX_VERSION, 0, -1):
        vid = f"{pmcid}.{version}"
        try:
            return vid, get_json(f"{BUCKET}/{vid}/{vid}.json")
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
    return None


def accept(meta: dict[str, Any]) -> bool:
    return (
        meta.get("is_pmc_openaccess") is True
        and meta.get("is_retracted") is False
        and meta.get("is_historical_ocr") is False
        and meta.get("license_code") in ALLOWED_LICENSES
    )


def download(url: str, dest: Path) -> tuple[int, str]:
    """Write url to dest; return (size in bytes, sha256 hex digest)."""
    req = urllib.request.Request(url, headers=USER_AGENT)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return len(data), hashlib.sha256(data).hexdigest()


def download_pair(vid: str, pdf_dest: Path, xml_dest: Path) -> dict[str, Any]:
    """Download PDF and XML together, or neither.

    Both files land under temporary names and are renamed only once both
    succeed, so a failure never leaves an unpaired file in the corpus.
    """
    parts = [
        (f"{BUCKET}/{vid}/{vid}.pdf", pdf_dest),
        (f"{BUCKET}/{vid}/{vid}.xml", xml_dest),
    ]
    temps = [dest.with_name(dest.name + ".part") for _, dest in parts]
    try:
        pdf_bytes, pdf_sha = download(parts[0][0], temps[0])
        xml_bytes, xml_sha = download(parts[1][0], temps[1])
    except BaseException:
        for temp in temps:
            temp.unlink(missing_ok=True)
        raise

    for temp, (_, dest) in zip(temps, parts, strict=True):
        temp.replace(dest)

    return {
        "pdf_bytes": pdf_bytes,
        "pdf_sha256": pdf_sha,
        "xml_bytes": xml_bytes,
        "xml_sha256": xml_sha,
    }


def count(skipped: dict[str, int], reason: str) -> None:
    skipped[reason] = skipped.get(reason, 0) + 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--journal", default="Frontiers in Neuroscience")
    parser.add_argument("--mindate", default="2020")
    parser.add_argument("--maxdate", default="2025")
    parser.add_argument("--count", type=int, default=10, help="articles to keep")
    parser.add_argument("--out", default="corpus")
    args = parser.parse_args()

    fetched_at = datetime.now(UTC).isoformat(timespec="seconds")
    out = Path(args.out)
    pdf_dir = out / "pdf"
    xml_dir = out / "xml"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    xml_dir.mkdir(parents=True, exist_ok=True)

    # Over-request: some candidates fail the filters or have no S3 prefix.
    candidates = search_pmcids(args.journal, args.mindate, args.maxdate, args.count * 3)
    print(f"esearch returned {len(candidates)} candidates")
    if not candidates:
        raise SystemExit("no candidates - check the journal name spelling")

    rows: list[dict[str, Any]] = []
    skipped: dict[str, int] = {}

    for pmcid in candidates:
        if len(rows) >= args.count:
            break

        time.sleep(REQUEST_DELAY)
        try:
            found = fetch_metadata(pmcid)
        except urllib.error.HTTPError as exc:
            count(skipped, f"metadata_{exc.code}")
            continue
        except NETWORK_ERRORS:
            count(skipped, "metadata_network_error")
            continue

        if found is None:
            count(skipped, "no_s3_prefix")
            continue

        vid, meta = found
        if not accept(meta):
            count(skipped, "failed_filter")
            continue

        try:
            files = download_pair(
                vid, pdf_dir / f"{pmcid}.pdf", xml_dir / f"{pmcid}.xml"
            )
        except urllib.error.HTTPError as exc:
            count(skipped, f"download_{exc.code}")
            continue
        except NETWORK_ERRORS:
            count(skipped, "download_network_error")
            continue

        rows.append(
            {
                "pmcid": pmcid,
                "versioned_id": vid,
                "version": meta.get("version"),
                "title": meta.get("title"),
                "citation": meta.get("citation"),
                "doi": meta.get("doi"),
                "license_code": meta.get("license_code"),
                **files,
            }
        )
        print(f"[{len(rows)}/{args.count}] {vid}  {str(meta.get('title'))[:60]}")

    if not rows:
        raise SystemExit(f"nothing accepted. skipped: {skipped}")

    manifest = out / "manifest.csv"
    with manifest.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    query = out / "query.json"
    query.write_text(
        json.dumps(
            {
                "fetched_at": fetched_at,
                "journal": args.journal,
                "mindate": args.mindate,
                "maxdate": args.maxdate,
                "filters": {
                    "is_pmc_openaccess": True,
                    "is_retracted": False,
                    "is_historical_ocr": False,
                    "license_code": sorted(ALLOWED_LICENSES),
                },
                "version_policy": f"latest of 1..{MAX_VERSION}",
                "candidates": len(candidates),
                "accepted": len(rows),
                "skipped": skipped,
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"\n{len(rows)} articles -> {out}")
    print(f"skipped: {skipped}")
    print(f"manifest: {manifest}")


if __name__ == "__main__":
    main()