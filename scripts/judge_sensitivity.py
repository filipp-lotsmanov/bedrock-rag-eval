"""Measure whether the groundedness judge catches errors it should catch.

    uv run python scripts/judge_sensitivity.py results/<run>    # ~2 min

Each probe in eval/judge_probes.yaml replaces the text of one reply from results/<run>/raw.jsonl
and keeps that reply's passages. Probes are judged with the same model and prompt as
judge_groundedness.py. A seeded probe is caught when any claim is rated partial or unsupported;
a control (a paraphrase that changes no facts) passes when every claim is rated supported.
Results go to results/<run>/sensitivity_<prompt hash>.jsonl; a probe whose text has changed
since it was judged is judged again.
"""

import argparse
import hashlib
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import yaml
from botocore.config import Config
from judge_groundedness import JUDGE_MODEL, judge_message, judge_one, latest_results, prompt_id

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

ROOT = Path(__file__).resolve().parents[1]
CONTROL = "control"


def load_probes(path: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [e for e in entries if e.get("review") != "dropped"]


def citations(text: str) -> list[str]:
    return sorted(re.findall(r"\[\d+\]", text))


def validate(probes: list[dict[str, Any]], results: dict[str, dict[str, Any]]) -> list[str]:
    """Problems that would make a probe measure something other than its stated change."""
    problems: list[str] = []
    for probe in probes:
        pid, record = probe["id"], results.get(probe["source_qid"])
        if record is None or record["result"]["abstained"]:
            problems.append(f"{pid}: source {probe['source_qid']} missing or abstained")
            continue
        source = record["result"]["text"]
        if probe["type"] == CONTROL:
            if probe["answer_text"] == source:
                problems.append(f"{pid}: control is identical to its source, so it tests nothing")
            if citations(probe["answer_text"]) != citations(source):
                problems.append(f"{pid}: control changes the citations")
            continue
        old, new = probe["edit"]["from"], probe["edit"]["to"]
        if source.count(old) != 1:
            problems.append(f"{pid}: edit 'from' occurs {source.count(old)} times in the source")
        elif probe["answer_text"] != source.replace(old, new):
            problems.append(f"{pid}: answer_text is not the source with exactly the stated edit")
    return problems


def answer_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:12]


def sensitivity_path(run_dir: Path) -> Path:
    return run_dir / f"sensitivity_{prompt_id()}.jsonl"


def load_sensitivity(path: Path, probes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Judged records whose text still matches the current probe."""
    if not path.exists():
        return {}
    current = {p["id"]: answer_sha(p["answer_text"]) for p in probes}
    records = (
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    return {
        r["id"]: r for r in records if "claims" in r and current.get(r["id"]) == r["answer_sha"]
    }


def run_probes(
    run_dir: Path,
    probes: list[dict[str, Any]],
    results: dict[str, dict[str, Any]],
    interval: float,
    client: "BedrockRuntimeClient | None" = None,
) -> None:
    path = sensitivity_path(run_dir)
    done = load_sensitivity(path, probes)
    todo = [p for p in probes if p["id"] not in done]
    if not todo:
        return
    client = client or boto3.client(
        "bedrock-runtime", config=Config(retries={"mode": "adaptive", "max_attempts": 6})
    )
    print(f"judging {len(todo)} probe(s) with {JUDGE_MODEL}, ~{len(todo) * interval / 60:.0f} min")
    with path.open("a", encoding="utf-8") as out:
        for number, probe in enumerate(todo, start=1):
            started = time.monotonic()
            source = results[probe["source_qid"]]["result"]
            record: dict[str, Any] = {
                "id": probe["id"],
                "source_qid": probe["source_qid"],
                "type": probe["type"],
                "answer_sha": answer_sha(probe["answer_text"]),
                "judge_model": JUDGE_MODEL,
                "judge_prompt_id": prompt_id(),
            }
            message = judge_message(source["question"], source | {"text": probe["answer_text"]})
            try:
                claims, raw, usage = judge_one(client, message)
                record |= {"claims": claims, "raw": raw, "usage": usage}
                verdicts = dict(Counter(c["verdict"] for c in claims))
                print(
                    f"[{number}/{len(todo)}] {probe['id']} ({probe['type']}) {verdicts}", flush=True
                )
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {error}"
                print(f"[{number}/{len(todo)}] {probe['id']} ERROR {record['error']}", flush=True)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


def passed(probe: dict[str, Any], claims: list[dict[str, Any]]) -> bool:
    """Seeded: caught if any claim is not fully supported. Control: every claim supported."""
    supported = [c["verdict"] == "supported" for c in claims]
    if probe["type"] == CONTROL:
        return bool(claims) and all(supported)
    return not all(supported)


def summarize(probes: list[dict[str, Any]], judged: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, dict[str, int]] = {}
    misses: list[dict[str, Any]] = []
    for probe in probes:
        if probe["id"] not in judged:
            continue
        claims = judged[probe["id"]]["claims"]
        tally = by_type.setdefault(probe["type"], {"passed": 0, "total": 0})
        tally["total"] += 1
        if passed(probe, claims):
            tally["passed"] += 1
        else:
            misses.append(
                {
                    "id": probe["id"],
                    "type": probe["type"],
                    "answer_text": probe["answer_text"],
                    "claims": [
                        {
                            "claim": c["claim"],
                            "verdict": c["verdict"],
                            "reason": c.get("reason", ""),
                        }
                        for c in claims
                    ],
                }
            )
    seeded = [t for name, t in by_type.items() if name != CONTROL]
    caught, total = sum(t["passed"] for t in seeded), sum(t["total"] for t in seeded)
    return {
        "judge_model": JUDGE_MODEL,
        "judge_prompt_id": prompt_id(),
        "probes_judged": len(judged),
        "seeded_caught": {"numerator": caught, "denominator": total},
        "controls_passed": by_type.get(CONTROL, {"passed": 0, "total": 0}),
        "by_type": by_type,
        "misses": misses,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "run", type=Path, help="results/<run> directory the probes were drafted from"
    )
    parser.add_argument("--probes", type=Path, default=ROOT / "eval" / "judge_probes.yaml")
    parser.add_argument("--interval", type=float, default=6.0, help="seconds between judge calls")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    probes = load_probes(args.probes)
    results = latest_results(args.run / "raw.jsonl")
    problems = validate(probes, results)
    if problems:
        print("probe file does not match this run:\n  " + "\n  ".join(problems))
        return 2
    run_probes(args.run, probes, results, args.interval)
    judged = load_sensitivity(sensitivity_path(args.run), probes)
    summary = summarize(probes, judged)
    (args.run / f"sensitivity_summary_{prompt_id()}.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    seeded, controls = summary["seeded_caught"], summary["controls_passed"]
    print(f"\nseeded errors caught {seeded['numerator']}/{seeded['denominator']}")
    for name, tally in sorted(summary["by_type"].items()):
        if name != CONTROL:
            print(f"  {name:<10} {tally['passed']}/{tally['total']}")
    print(f"controls passed {controls['passed']}/{controls['total']}")
    for miss in summary["misses"]:
        print(f"\n  {miss['id']} ({miss['type']}) {miss['answer_text']}")
        for claim in miss["claims"]:
            print(f"    [{claim['verdict']}] {claim['claim']}\n        {claim['reason']}")
    missing = len(probes) - len(judged)
    if missing:
        print(f"{missing} probe(s) not judged yet (errors); rerun the same command to retry them")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
