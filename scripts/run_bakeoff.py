"""Run the PDF parser bake-off against JATS ground truth.

For every article in corpus/manifest.csv and every parser, extract per-page
text, split and normalise it, and score it against the ground truth from
the article's JATS XML. Parsers run with their default settings: tuning one
and not the other would bias the comparison.

Writes two files per run to results/:
    bakeoff-<run_id>.csv    one row per (article, parser)
    bakeoff-<run_id>.json   parser versions, git commit, manifest hash, settings

Usage:
    uv run python scripts/run_bakeoff.py
    uv run python scripts/run_bakeoff.py --repeats 1
"""

import argparse
import csv
import hashlib
import json
import math
import platform
import statistics
import subprocess
import time
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from typing import Any

from bakeoff import extract_ground_truth, order_score, recovery, split_sentences
from bakeoff.parsers import PARSERS, Extractor

THRESHOLDS = (0.90, 0.95, 0.99)
ORDER_THRESHOLD = 0.95
"""order_score is computed from the matches at this threshold only."""

FIELDS = [
    "pmcid",
    "parser",
    "pages",
    "truth_sentences",
    "extracted_sentences",
    *(f"recovery_{round(t * 100)}" for t in THRESHOLDS),
    f"order_{round(ORDER_THRESHOLD * 100)}",
    "seconds_median",
    "error",
]


def timed_extract(extract: Extractor, path: Path, repeats: int) -> tuple[list[str], float]:
    """Run the extractor repeats times; return the last output and the median time.

    Only extraction is timed. The median damps disk-cache and scheduler noise,
    which dominates single runs on a laptop.
    """
    times: list[float] = []
    pages: list[str] = []
    for _ in range(repeats):
        start = time.perf_counter()
        pages = extract(path)
        times.append(time.perf_counter() - start)
    return pages, statistics.median(times)


def score(truth: list[str], pages: list[str]) -> dict[str, Any]:
    """Join pages with newlines so unwrap() can repair line-break hyphenation."""
    extracted = split_sentences("\n".join(pages))
    row: dict[str, Any] = {"pages": len(pages), "extracted_sentences": len(extracted)}
    for threshold in THRESHOLDS:
        fraction, matched = recovery(truth, extracted, threshold)
        row[f"recovery_{round(threshold * 100)}"] = round(fraction, 4)
        if threshold == ORDER_THRESHOLD:
            order = order_score(matched)
            row[f"order_{round(threshold * 100)}"] = "" if math.isnan(order) else round(order, 4)
    return row


def git_state() -> dict[str, Any]:
    """Commit and dirty flag, so results trace back to the exact code.

    Returns nulls instead of raising: a missing git must not discard a
    finished run.
    """

    def run(*args: str) -> str | None:
        try:
            result = subprocess.run(["git", *args], capture_output=True, text=True, check=False)
        except FileNotFoundError:
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    status = run("status", "--porcelain")
    return {"commit": run("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", default="corpus")
    parser.add_argument("--out", default="results")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    corpus = Path(args.corpus)
    manifest = corpus / "manifest.csv"
    with manifest.open(encoding="utf-8") as fh:
        articles = list(csv.DictReader(fh))

    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out)
    out.mkdir(exist_ok=True)

    rows: list[dict[str, Any]] = []
    for article in articles:
        pmcid = article["pmcid"]
        truth = split_sentences(extract_ground_truth(corpus / "xml" / f"{pmcid}.xml"))
        pdf_path = corpus / "pdf" / f"{pmcid}.pdf"

        for name, extract in PARSERS.items():
            row: dict[str, Any] = {
                "pmcid": pmcid,
                "parser": name,
                "truth_sentences": len(truth),
                "error": "",
            }
            try:
                pages, seconds = timed_extract(extract, pdf_path, args.repeats)
                row.update(score(truth, pages))
                row["seconds_median"] = round(seconds, 3)
            except Exception as exc:
                row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            print(
                f"{pmcid} {name:<11} recovery@95={row.get('recovery_95', '-')}  "
                f"order={row.get('order_95', '-')}  {row['error']}"
            )

    csv_path = out / f"bakeoff-{run_id}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS, restval="")
        writer.writeheader()
        writer.writerows(rows)

    meta = {
        "run_id": run_id,
        "git": git_state(),
        "manifest_sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "articles": len(articles),
        "parsers": {name: version(name) for name in PARSERS},
        "python": platform.python_version(),
        "platform": platform.platform(),
        "thresholds": list(THRESHOLDS),
        "order_threshold": ORDER_THRESHOLD,
        "repeats": args.repeats,
        "settings": "parser defaults",
    }
    json_path = out / f"bakeoff-{run_id}.json"
    json_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")

    print("\nparser       mean@90  mean@95  mean@99  min@95  errors")
    for name in PARSERS:
        ok = [r for r in rows if r["parser"] == name and not r["error"]]
        errors = sum(1 for r in rows if r["parser"] == name and r["error"])
        if not ok:
            print(f"{name:<11}  no successful runs  errors={errors}")
            continue
        means = [statistics.mean(r[f"recovery_{round(t * 100)}"] for r in ok) for t in THRESHOLDS]
        worst = min(r["recovery_95"] for r in ok)
        print(
            f"{name:<11}  {means[0]:.3f}    {means[1]:.3f}    {means[2]:.3f}    "
            f"{worst:.3f}   {errors}"
        )
    print(f"\n{csv_path}\n{json_path}")


if __name__ == "__main__":
    main()
