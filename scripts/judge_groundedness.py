"""Judge whether each answered reply is grounded in the passages it cites.

    uv run python scripts/judge_groundedness.py results/<run>                 # judge, ~4 min
    uv run python scripts/judge_groundedness.py results/<run> --calibrate 20  # then hand-grade

A judge model (Nova Pro, stronger than the Nova Lite it judges) splits each reply into claims
and rates every claim against the passages that claim cites. Calibration shows you replies
with the judge's verdicts hidden, records your verdicts, and reports how often you agree.
Results go to results/<run>/judge_<prompt hash>.jsonl, so a changed judge prompt never mixes
with earlier judgements. Abstentions are not judged: they make no claims.
"""

import argparse
import hashlib
import json
import random
import re
import sys
import textwrap
import time
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import yaml
from botocore.config import Config

if TYPE_CHECKING:
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

ROOT = Path(__file__).resolve().parents[1]
JUDGE_MODEL = "eu.amazon.nova-pro-v1:0"
VERDICTS = ("supported", "partial", "unsupported")
SHORT = {"s": "supported", "p": "partial", "u": "unsupported"}

# [H] The judge prompt defines the groundedness metric: review it like the system prompt.
JUDGE_PROMPT = """\
You check whether an answer is supported by source passages. You receive numbered passages, \
a question, and an answer that cites passages as [n].

Split the answer into its individual factual claims: usually one per sentence, but split a \
sentence that makes several independent claims. Skip the token INSUFFICIENT_CONTEXT and \
statements that something is not covered by the passages.

Rate each claim only against the passages it cites:
- supported: the cited passages state the claim, or it follows directly from them
- partial: the cited passages support the claim only in part; a number, scope or qualifier \
is missing or different
- unsupported: the cited passages do not support the claim, or the claim cites nothing

Do not use outside knowledge. A claim that is true but not supported by its cited passages \
is unsupported.

Reply with JSON only, no other text:
{"claims": [{"claim": "...", "cited": [1], "verdict": "supported", "reason": "..."}]}
"""


def prompt_id() -> str:
    return hashlib.sha256(f"{JUDGE_MODEL}\n{JUDGE_PROMPT}".encode()).hexdigest()[:12]


def judge_path(run_dir: Path) -> Path:
    return run_dir / f"judge_{prompt_id()}.jsonl"


def latest_results(raw: Path) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for line in raw.read_text(encoding="utf-8").splitlines():
        if line.strip() and "result" in (record := json.loads(line)):
            records[record["id"]] = record
    return records


def answered(run_dir: Path, questions_path: Path) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    entries: list[dict[str, Any]] = yaml.safe_load(questions_path.read_text(encoding="utf-8"))
    questions = {e["id"]: e for e in entries if e["review"] != "dropped"}
    results = latest_results(run_dir / "raw.jsonl")
    return [
        (questions[qid], record["result"])
        for qid, record in results.items()
        if qid in questions and not record["result"]["abstained"]
    ]


def judge_message(question: str, result: dict[str, Any]) -> str:
    passages = "\n\n".join(f"[{n}] {p['text']}" for n, p in enumerate(result["passages"], start=1))
    return f"Passages:\n\n{passages}\n\nQuestion: {question}\n\nAnswer: {result['text']}"


def parse_claims(text: str) -> list[dict[str, Any]]:
    """The judge's JSON, tolerating a code fence around it; raises ValueError if malformed."""
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError("no JSON object in the judge's reply")
    claims = json.loads(match.group(0)).get("claims")
    if not isinstance(claims, list):
        raise ValueError("judge reply has no claims list")
    for claim in claims:
        if claim.get("verdict") not in VERDICTS or not str(claim.get("claim", "")).strip():
            raise ValueError(f"malformed claim: {claim!r}")
        claim["cited"] = [int(n) for n in claim.get("cited", [])]
    return claims


def judge_one(
    client: "BedrockRuntimeClient", message: str
) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    last_error: ValueError | None = None
    for _ in range(2):
        response = client.converse(
            modelId=JUDGE_MODEL,
            system=[{"text": JUDGE_PROMPT}],
            messages=[{"role": "user", "content": [{"text": message}]}],
            inferenceConfig={"maxTokens": 1500, "temperature": 0.0},
        )
        content = response["output"].get("message", {}).get("content", [])
        text = "".join(block.get("text", "") for block in content)
        usage = {
            "input_tokens": response["usage"]["inputTokens"],
            "output_tokens": response["usage"]["outputTokens"],
        }
        try:
            return parse_claims(text), text, usage
        except (ValueError, json.JSONDecodeError) as error:
            last_error = ValueError(str(error))
    raise last_error or ValueError("judge failed")


def load_judgements(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    lines = (
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    return {line["id"]: line for line in lines if "claims" in line}


def summarize(judgements: dict[str, dict[str, Any]]) -> dict[str, Any]:
    claims = [c for j in judgements.values() for c in j["claims"]]
    counts = Counter(c["verdict"] for c in claims)
    with_claims = [j for j in judgements.values() if j["claims"]]
    grounded = [j for j in with_claims if all(c["verdict"] == "supported" for c in j["claims"])]
    return {
        "judge_model": JUDGE_MODEL,
        "judge_prompt_id": prompt_id(),
        "answers_judged": len(judgements),
        "claims": len(claims),
        "verdicts": {v: counts.get(v, 0) for v in VERDICTS},
        "claim_support_rate": round(counts.get("supported", 0) / len(claims), 3)
        if claims
        else None,
        "fully_grounded_answers": {"numerator": len(grounded), "denominator": len(with_claims)},
        "not_fully_supported": [
            {
                "id": j["id"],
                "claim": c["claim"],
                "verdict": c["verdict"],
                "reason": c.get("reason", ""),
            }
            for j in judgements.values()
            for c in j["claims"]
            if c["verdict"] != "supported"
        ],
    }


def run_judge(
    run_dir: Path, items: list[tuple[dict[str, Any], dict[str, Any]]], interval: float
) -> None:
    path = judge_path(run_dir)
    done = load_judgements(path)
    todo = [(q, r) for q, r in items if q["id"] not in done]
    if not todo:
        return
    client = boto3.client(
        "bedrock-runtime", config=Config(retries={"mode": "adaptive", "max_attempts": 6})
    )
    print(f"judging {len(todo)} repl(ies) with {JUDGE_MODEL}, ~{len(todo) * interval / 60:.0f} min")
    with path.open("a", encoding="utf-8") as out:
        for number, (question, result) in enumerate(todo, start=1):
            started = time.monotonic()
            record: dict[str, Any] = {
                "id": question["id"],
                "judge_model": JUDGE_MODEL,
                "judge_prompt_id": prompt_id(),
            }
            try:
                claims, raw, usage = judge_one(client, judge_message(question["question"], result))
                record |= {"claims": claims, "raw": raw, "usage": usage}
                verdicts = dict(Counter(c["verdict"] for c in claims))
                print(f"[{number}/{len(todo)}] {question['id']} {verdicts}", flush=True)
            except Exception as error:
                record["error"] = f"{type(error).__name__}: {error}"
                print(
                    f"[{number}/{len(todo)}] {question['id']} ERROR {record['error']}", flush=True
                )
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            time.sleep(max(0.0, interval - (time.monotonic() - started)))


def agreement(judgements: dict[str, dict[str, Any]], human: dict[str, list[str]]) -> dict[str, Any]:
    pairs = [
        (claim["verdict"], verdict)
        for qid, verdicts in human.items()
        for claim, verdict in zip(judgements[qid]["claims"], verdicts, strict=True)
    ]
    exact = sum(j == h for j, h in pairs)
    binary = sum((j == "supported") == (h == "supported") for j, h in pairs)
    return {
        "answers": len(human),
        "claims": len(pairs),
        "exact_agreement": round(exact / len(pairs), 3) if pairs else None,
        "supported_vs_not_agreement": round(binary / len(pairs), 3) if pairs else None,
        "confusion_judge_vs_human": dict(Counter(f"{j}/{h}" for j, h in pairs)),
    }


def calibrate(
    run_dir: Path, judgements: dict[str, dict[str, Any]], results: dict[str, dict[str, Any]], n: int
) -> dict[str, list[str]]:
    """Blind hand-grading: the judge's verdicts stay hidden until the summary."""
    path = run_dir / f"human_{prompt_id()}.json"
    human: dict[str, list[str]] = (
        json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    )
    candidates = sorted(qid for qid, j in judgements.items() if j["claims"])
    sample = random.Random(run_dir.name).sample(candidates, min(n, len(candidates)))
    for qid in (q for q in sample if q not in human):
        result, claims = results[qid]["result"], judgements[qid]["claims"]
        print(f"\n{'=' * 100}\n{qid}: {result['question']}\nAnswer: {result['text']}")
        for number in sorted({n for c in claims for n in c["cited"]}):
            if 1 <= number <= len(result["passages"]):
                print(
                    textwrap.indent(
                        textwrap.fill(f"[{number}] {result['passages'][number - 1]['text']}", 97),
                        "   ",
                    )
                )
        verdicts: list[str] = []
        for index, claim in enumerate(claims, start=1):
            answer = ""
            while answer not in SHORT and answer != "q":
                answer = (
                    input(
                        f"\nClaim {index}: {claim['claim']} (cites {claim['cited']})\n[s]upported  [p]artial  [u]nsupported  [q]uit > "
                    )
                    .strip()
                    .lower()
                )
            if answer == "q":
                return human
            verdicts.append(SHORT[answer])
        human[qid] = verdicts
        path.write_text(json.dumps(human, indent=2) + "\n", encoding="utf-8")
    return human


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("run", type=Path, help="results/<run> directory")
    parser.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.yaml")
    parser.add_argument("--interval", type=float, default=6.0, help="seconds between judge calls")
    parser.add_argument("--calibrate", type=int, metavar="N", help="hand-grade N judged replies")
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    items = answered(args.run, args.questions)
    run_judge(args.run, items, args.interval)
    judgements = load_judgements(judge_path(args.run))
    summary = summarize(judgements)
    if args.calibrate:
        human = calibrate(
            args.run, judgements, latest_results(args.run / "raw.jsonl"), args.calibrate
        )
        summary["human_agreement"] = agreement(judgements, human)
    (args.run / f"judge_summary_{prompt_id()}.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    rate = summary["claim_support_rate"]
    grounded = summary["fully_grounded_answers"]
    print(
        f"\n{summary['answers_judged']} answers, {summary['claims']} claims: {summary['verdicts']}"
    )
    print(
        f"claim support rate {rate}; fully grounded answers {grounded['numerator']}/{grounded['denominator']}"
    )
    for item in summary["not_fully_supported"]:
        print(f"  {item['id']} [{item['verdict']}] {item['claim']}\n      {item['reason']}")
    if "human_agreement" in summary:
        print(f"human agreement: {summary['human_agreement']}")
    missing = len(items) - len(judgements)
    if missing:
        print(f"{missing} repl(ies) not judged yet (errors); rerun the same command to retry them")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
