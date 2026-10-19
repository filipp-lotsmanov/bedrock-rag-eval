"""Compute evaluation metrics from a run's raw results. Costs nothing: no model is called.

    uv run python scripts/compute_metrics.py results/<run>            # metrics.json + table
    uv run python scripts/compute_metrics.py results/<run> --grade    # grade false premises first
    uv run python scripts/compute_metrics.py results/<v0> results/<v1>    # side by side

Definitions (answerable = category "answerable"):
    doc_hit@k            answerable: a retrieved passage comes from an expected paper
    page_hit@k           answerable: such a passage's page range overlaps an expected page
    mrr                  answerable: mean of 1 / rank of the first page hit (0 when none)
    answer_rate          answerable: share answered rather than abstained
    correct_abstention   not_covered and off_topic: share abstained
    premise_handling     false_premise: share abstained or graded as correcting the premise
    citation_validity    answered questions: share citing only passages that were shown
    citation_on_evidence answered answerable: share with a citation on an expected page.
                         A lower bound: a citation elsewhere can still be valid support.
Abstention is taken as recorded in the run, i.e. as the code behaved at the time.
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
GRADES_FILE = "premise_grades.json"
METRICS = (
    "doc_hit@k",
    "page_hit@k",
    "mrr",
    "answer_rate",
    "correct_abstention",
    "premise_handling",
    "citation_validity",
    "citation_on_evidence",
)


def rate(numerator: int, denominator: int) -> dict[str, Any]:
    value = round(numerator / denominator, 3) if denominator else None
    return {"value": value, "numerator": numerator, "denominator": denominator}


def latest_results(raw: Path) -> dict[str, dict[str, Any]]:
    """The last successful record per question; earlier errored attempts are ignored."""
    records: dict[str, dict[str, Any]] = {}
    for line in raw.read_text(encoding="utf-8").splitlines():
        if line.strip() and "result" in (record := json.loads(line)):
            records[record["id"]] = record
    return records


def overlaps(start: int, end: int, pages: list[int]) -> bool:
    return any(start <= page <= end for page in pages)


def on_evidence(item: dict[str, Any], question: dict[str, Any]) -> bool:
    return item["doc_id"] in question["expected_docs"] and overlaps(
        item["page_start"], item["page_end"], question["expected_pages"]
    )


def first_hit_rank(passages: list[dict[str, Any]], question: dict[str, Any]) -> int | None:
    for rank, passage in enumerate(passages, start=1):
        if on_evidence(passage, question):
            return rank
    return None


def per_question(
    question: dict[str, Any], result: dict[str, Any], grade: bool | None
) -> dict[str, Any]:
    category, abstained = question["category"], result["abstained"]
    row: dict[str, Any] = {"id": question["id"], "category": category, "abstained": abstained}
    if category == "answerable":
        rank = first_hit_rank(result["passages"], question)
        row |= {
            "doc_hit": any(p["doc_id"] in question["expected_docs"] for p in result["passages"]),
            "page_hit": rank is not None,
            "reciprocal_rank": 1 / rank if rank else 0.0,
            "top_score": result["passages"][0]["score"] if result["passages"] else None,
        }
        if not abstained:
            row["citation_on_evidence"] = any(on_evidence(c, question) for c in result["citations"])
    if not abstained:
        row["citation_valid"] = not result["invalid_citations"]
    if category == "false_premise":
        row["premise_correct"] = True if abstained else grade
    return row


def summarize(rows: list[dict[str, Any]], k: int) -> dict[str, Any]:
    answerable = [r for r in rows if r["category"] == "answerable"]
    refusable = [r for r in rows if r["category"] in {"not_covered", "off_topic"}]
    premises = [r for r in rows if r["category"] == "false_premise"]
    answered = [r for r in rows if "citation_valid" in r]
    cited = [r for r in answerable if "citation_on_evidence" in r]
    graded = [r for r in premises if r["premise_correct"] is not None]
    return {
        "k": k,
        "doc_hit@k": rate(sum(r["doc_hit"] for r in answerable), len(answerable)),
        "page_hit@k": rate(sum(r["page_hit"] for r in answerable), len(answerable)),
        "mrr": {
            "value": round(sum(r["reciprocal_rank"] for r in answerable) / len(answerable), 3)
            if answerable
            else None,
            "denominator": len(answerable),
        },
        "answer_rate": rate(sum(not r["abstained"] for r in answerable), len(answerable)),
        "correct_abstention": rate(sum(r["abstained"] for r in refusable), len(refusable)),
        "premise_handling": rate(sum(bool(r["premise_correct"]) for r in graded), len(premises))
        | {"ungraded": len(premises) - len(graded)},
        "citation_validity": rate(sum(r["citation_valid"] for r in answered), len(answered)),
        "citation_on_evidence": rate(sum(r["citation_on_evidence"] for r in cited), len(cited)),
    }


def load_questions(
    path: Path, run: dict[str, Any], allow_label_change: bool
) -> dict[str, dict[str, Any]]:
    if (
        hashlib.sha256(path.read_bytes()).hexdigest() != run["questions_sha256"]
        and not allow_label_change
    ):
        raise SystemExit(
            f"{path} changed since run {run['run_id']}; its labels may no longer match "
            "(pass --allow-label-change to score with the current labels)"
        )
    entries: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {e["id"]: e for e in entries if e["review"] != "dropped"}


def grade_premises(
    run_dir: Path, questions: dict[str, dict[str, Any]], results: dict[str, dict[str, Any]]
) -> None:
    grades_path = run_dir / GRADES_FILE
    grades: dict[str, bool] = (
        json.loads(grades_path.read_text(encoding="utf-8")) if grades_path.exists() else {}
    )
    for qid, question in questions.items():
        result = results.get(qid, {}).get("result")
        if (
            question["category"] != "false_premise"
            or result is None
            or result["abstained"]
            or qid in grades
        ):
            continue
        print(
            f"\n{qid}: {question['question']}\nTruth: {question['answer_sketch']}\nReply: {result['text']}"
        )
        verdict = (
            input("Does the reply correct the premise? [y]es  [n]o  [s]kip > ").strip().lower()
        )
        if verdict in {"y", "n"}:
            grades[qid] = verdict == "y"
            grades_path.write_text(
                json.dumps(grades, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )


def compute(
    run_dir: Path, questions_path: Path, allow_label_change: bool, grade: bool
) -> dict[str, Any]:
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    questions = load_questions(questions_path, run, allow_label_change)
    results = latest_results(run_dir / "raw.jsonl")
    if grade:
        grade_premises(run_dir, questions, results)
    grades_path = run_dir / GRADES_FILE
    grades: dict[str, bool] = (
        json.loads(grades_path.read_text(encoding="utf-8")) if grades_path.exists() else {}
    )
    rows = [
        per_question(question, results[qid]["result"], grades.get(qid))
        for qid, question in questions.items()
        if qid in results
    ]
    latencies = [results[r["id"]]["latency_s"] for r in rows]
    usage = [results[r["id"]]["result"]["usage"] for r in rows]
    metrics = {
        "run_id": run["run_id"],
        "config_id": run["config_id"],
        "git": run["git"],
        "questions_scored": len(rows),
        "questions_missing": sorted(set(questions) - set(results)),
        "summary": summarize(rows, run["config"]["top_k"]),
        "latency_s": {"mean": round(sum(latencies) / len(latencies), 3), "max": max(latencies)}
        if latencies
        else None,
        "tokens": {
            "input": sum(u.get("input_tokens", 0) for u in usage),
            "output": sum(u.get("output_tokens", 0) for u in usage),
        },
        "per_question": rows,
    }
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8")
    return metrics


def cell(metric: dict[str, Any]) -> str:
    if metric["value"] is None:
        return "-"
    if "numerator" not in metric:
        return f"{metric['value']:.3f}"
    note = f" +{metric['ungraded']} ungraded" if metric.get("ungraded") else ""
    return f"{metric['value']:.3f} ({metric['numerator']}/{metric['denominator']}){note}"


def print_table(all_metrics: list[dict[str, Any]]) -> None:
    names = [m["run_id"][:16] for m in all_metrics]
    width = max(26, *(len(n) for n in names))
    print(f"\n{'metric':<22}" + "".join(f"{n:<{width}}" for n in names))
    for name in METRICS:
        print(f"{name:<22}" + "".join(f"{cell(m['summary'][name]):<{width}}" for m in all_metrics))
    for m in all_metrics:
        if m["questions_missing"]:
            print(f"{m['run_id']}: no result for {', '.join(m['questions_missing'])}")
    print("\ncitation_on_evidence is a lower bound; see the docstring.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("runs", nargs="+", type=Path, help="one or more results/<run> directories")
    parser.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.yaml")
    parser.add_argument(
        "--grade", action="store_true", help="grade answered false-premise replies first"
    )
    parser.add_argument(
        "--allow-label-change", action="store_true", help="score with an edited questions file"
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    all_metrics = [
        compute(run, args.questions, args.allow_label_change, args.grade) for run in args.runs
    ]
    print_table(all_metrics)
    return 0


if __name__ == "__main__":
    sys.exit(main())
