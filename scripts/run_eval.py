"""Ask every eval question through answer() and save the raw results with their provenance.

    uv run python scripts/run_eval.py                          # full run, about 20 min
    uv run python scripts/run_eval.py --ids q01,u08            # smoke test
    uv run python scripts/run_eval.py --resume results/<run>   # continue an interrupted run
    uv run python scripts/run_eval.py --no-rerank              # ablation: vector search only

Pacing: Amazon Rerank allows 2 requests per minute on this account, so reranked runs space
calls 31 s apart; without reranking, Nova Lite's 20 requests per minute allow 3.1 s.
Output in results/<run_id>/: run.json (provenance) and raw.jsonl (one line per attempt).
No metrics are computed here; they are derived from raw.jsonl separately, so they can be
recomputed without paying for another run.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import yaml
from botocore.config import Config
from botocore.exceptions import ClientError

from query import prompt
from query.answer import answer
from query.config import DEFAULT_QUERY_CONFIG, QueryConfig

if TYPE_CHECKING:
    from mypy_boto3_bedrock_agent_runtime import AgentsforBedrockRuntimeClient
    from mypy_boto3_bedrock_runtime import BedrockRuntimeClient

ROOT = Path(__file__).resolve().parents[1]
RERANK_INTERVAL_S = 31.0
GENERATE_INTERVAL_S = 3.1
THROTTLING = frozenset(
    {"ThrottlingException", "TooManyRequestsException", "ServiceQuotaExceededException"}
)
THROTTLE_RETRIES = 3
THROTTLE_WAIT_S = 65.0
MAX_CONSECUTIVE_ERRORS = 3
# Fields that must match before results from two sessions may share one run directory.
RESUME_KEYS = ("config_id", "questions_sha256", "system_prompt_sha256", "knowledge_base_id")


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_questions(path: Path, ids: set[str] | None, allow_pending: bool) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = yaml.safe_load(path.read_text(encoding="utf-8"))
    active = [e for e in entries if e["review"] != "dropped"]
    pending = [e["id"] for e in active if e["review"] == "pending"]
    if pending and not allow_pending:
        raise SystemExit(
            f"{len(pending)} question(s) pending review; finish the review or pass --allow-pending"
        )
    if ids is not None:
        unknown = ids - {e["id"] for e in active}
        if unknown:
            raise SystemExit(f"unknown or dropped ids: {', '.join(sorted(unknown))}")
        active = [e for e in active if e["id"] in ids]
    return active


def git_state() -> dict[str, Any]:
    def git(*args: str) -> str | None:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip() if result.returncode == 0 else None

    status = git("status", "--porcelain")
    return {"commit": git("rev-parse", "HEAD"), "dirty": None if status is None else bool(status)}


def knowledge_base_id(explicit: str | None) -> str:
    if explicit:
        return explicit
    result = subprocess.run(
        ["terraform", f"-chdir={ROOT / 'infra'}", "output", "-raw", "knowledge_base_id"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise SystemExit("could not read knowledge_base_id from terraform output; pass --kb-id")
    return result.stdout.strip()


def provenance(
    config: QueryConfig, kb_id: str, questions_path: Path, system_prompt: str
) -> dict[str, Any]:
    last_sync = ROOT / "build" / "last_sync.json"
    return {
        "config_id": config.config_id,
        "config": asdict(config),
        "knowledge_base_id": kb_id,
        "questions_file": str(
            questions_path.relative_to(ROOT)
            if questions_path.is_relative_to(ROOT)
            else questions_path
        ),
        "questions_sha256": sha256(questions_path.read_bytes()),
        "system_prompt_sha256": sha256(system_prompt.encode()),
        "system_prompt": system_prompt,
        "git": git_state(),
        "last_sync": json.loads(last_sync.read_text(encoding="utf-8"))
        if last_sync.exists()
        else None,
    }


def completed_ids(raw: Path) -> set[str]:
    """Questions with at least one successful line; errored attempts are asked again."""
    if not raw.exists():
        return set()
    lines = (
        json.loads(line) for line in raw.read_text(encoding="utf-8").splitlines() if line.strip()
    )
    return {line["id"] for line in lines if "result" in line}


def write_run(run_dir: Path, run: dict[str, Any]) -> None:
    (run_dir / "run.json").write_text(
        json.dumps(run, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check_resume(run: dict[str, Any], current: dict[str, Any]) -> None:
    changed = [key for key in RESUME_KEYS if run.get(key) != current[key]]
    if changed:
        raise SystemExit(
            f"cannot resume: {', '.join(changed)} changed since the run started; start a new run"
        )


class Pacer:
    """Keeps at least `interval` seconds between the starts of consecutive calls."""

    def __init__(self, interval: float) -> None:
        self.interval = interval
        self.last: float | None = None

    def wait(self) -> None:
        if self.last is not None:
            time.sleep(max(0.0, self.interval - (time.monotonic() - self.last)))
        self.last = time.monotonic()


def ask(
    question: dict[str, Any],
    kb_id: str,
    config: QueryConfig,
    clients: tuple["AgentsforBedrockRuntimeClient", "BedrockRuntimeClient"],
    pacer: Pacer,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "id": question["id"],
        "category": question["category"],
        "expected_behavior": question["expected_behavior"],
    }
    for attempt in range(THROTTLE_RETRIES + 1):
        pacer.wait()
        record["started_at"] = utc_now()
        start = time.perf_counter()
        try:
            result = answer(question["question"], kb_id, config, *clients)
        except ClientError as error:
            code = error.response.get("Error", {}).get("Code", "")
            if code in THROTTLING and attempt < THROTTLE_RETRIES:
                print(f"  throttled ({code}); waiting {THROTTLE_WAIT_S:.0f} s", flush=True)
                time.sleep(THROTTLE_WAIT_S)
                continue
            return {**record, "error": f"{code or type(error).__name__}: {error}"}
        except Exception as error:
            return {**record, "error": f"{type(error).__name__}: {error}"}
        return {
            **record,
            "latency_s": round(time.perf_counter() - start, 3),
            "result": result.to_dict(),
        }
    raise AssertionError("unreachable: the last attempt always returns")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--questions", type=Path, default=ROOT / "eval" / "questions.yaml")
    parser.add_argument("--ids", help="comma-separated subset, e.g. q01,u08")
    parser.add_argument("--resume", type=Path, help="run directory to continue")
    parser.add_argument("--no-rerank", action="store_true", help="vector search only (ablation)")
    parser.add_argument("--kb-id", help="default: terraform output knowledge_base_id")
    parser.add_argument(
        "--interval", type=float, help="seconds between calls (default from quotas)"
    )
    parser.add_argument(
        "--allow-pending", action="store_true", help="run even if some questions are unreviewed"
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if not prompt.SYSTEM_PROMPT:
        raise SystemExit("query/prompt.py has no SYSTEM_PROMPT")
    config = (
        replace(DEFAULT_QUERY_CONFIG, rerank_model_arn=None)
        if args.no_rerank
        else DEFAULT_QUERY_CONFIG
    )
    ids = set(args.ids.split(",")) if args.ids else None
    questions = load_questions(args.questions, ids, args.allow_pending)
    kb_id = knowledge_base_id(args.kb_id)
    current = provenance(config, kb_id, args.questions, prompt.SYSTEM_PROMPT)

    if args.resume:
        run_dir = args.resume
        run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
        check_resume(run, current)
    else:
        run_id = f"{datetime.now(UTC):%Y%m%dT%H%M%SZ}_{config.config_id}"
        run_dir = ROOT / "results" / run_id
        run_dir.mkdir(parents=True)
        run = {"run_id": run_id, "started_at": utc_now(), **current, "sessions": []}
    run["sessions"].append({"started_at": utc_now(), "git": current["git"]})
    run["status"] = "running"
    write_run(run_dir, run)
    raw = run_dir / "raw.jsonl"
    done = completed_ids(raw)
    todo = [q for q in questions if q["id"] not in done]
    if current["git"]["dirty"]:
        print("warning: uncommitted changes; the recorded commit does not fully describe this run")

    interval = (
        args.interval
        if args.interval is not None
        else (RERANK_INTERVAL_S if config.rerank_model_arn else GENERATE_INTERVAL_S)
    )
    retry = Config(retries={"mode": "adaptive", "max_attempts": 6}, read_timeout=60)
    clients = (
        boto3.client("bedrock-agent-runtime", config=retry),
        boto3.client("bedrock-runtime", config=retry),
    )
    print(
        f"run {run_dir.name}: {len(todo)} to ask, {len(done)} already done, ~{len(todo) * interval / 60:.0f} min"
    )

    pacer = Pacer(interval)
    try:
        ask_all(todo, kb_id, config, clients, pacer, raw, run_dir)
    except KeyboardInterrupt:
        print(f"\ninterrupted; continue with --resume {run_dir}")
    finally:
        finished = completed_ids(raw)
        expected = {q["id"] for q in questions}
        run.update(
            finished_at=utc_now(),
            question_count=len(expected),
            completed_count=len(expected & finished),
            status="complete" if expected <= finished else "incomplete",
        )
        write_run(run_dir, run)
    print(
        f"{run['status']}: {run['completed_count']}/{run['question_count']} answered -> {run_dir}"
    )
    return 0 if run["status"] == "complete" else 1


def ask_all(
    todo: list[dict[str, Any]],
    kb_id: str,
    config: QueryConfig,
    clients: tuple["AgentsforBedrockRuntimeClient", "BedrockRuntimeClient"],
    pacer: Pacer,
    raw: Path,
    run_dir: Path,
) -> None:
    consecutive_errors = 0
    with raw.open("a", encoding="utf-8") as out:
        for number, question in enumerate(todo, start=1):
            record = ask(question, kb_id, config, clients, pacer)
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            if "error" in record:
                consecutive_errors += 1
                print(
                    f"[{number}/{len(todo)}] {question['id']} ERROR {record['error']}", flush=True
                )
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    print(
                        f"stopping after {consecutive_errors} errors in a row; fix the cause, then --resume {run_dir}"
                    )
                    break
            else:
                consecutive_errors = 0
                result = record["result"]
                outcome = (
                    "abstained"
                    if result["abstained"]
                    else f"{len(result['citations'])} citation(s)"
                )
                print(
                    f"[{number}/{len(todo)}] {question['id']} {outcome} ({record['latency_s']} s)",
                    flush=True,
                )


if __name__ == "__main__":
    sys.exit(main())
