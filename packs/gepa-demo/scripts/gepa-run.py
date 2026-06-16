#!/usr/bin/env python3
"""Small fast-agent + GEPA demo loop.

`--evaluate-only` runs a no-network passthrough smoke test by default. For a
real optimization, install GEPA and pass a non-passthrough task model.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ENV_ROOT = Path(__file__).resolve().parents[1]
RUN_ROOT = ENV_ROOT / "gepa-runs"
DATA_DIR = ENV_ROOT / "data"
SEED_PATH = ENV_ROOT / "seed" / "instructions.md"
INPUT_PATH = DATA_DIR / "input.jsonl"
TASK_TEMPLATE = DATA_DIR / "task-template.md"
SMOKE_TEMPLATE = DATA_DIR / "smoke-template.md"
SCHEMA_PATH = DATA_DIR / "output.schema.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default="passthrough", help="Task model for fast-agent batch.")
    parser.add_argument("--reflection-lm", default="openai/gpt-5", help="GEPA reflection LM.")
    parser.add_argument("--max-metric-calls", type=int, default=12, help="GEPA evaluation budget.")
    parser.add_argument("--evaluate-only", action="store_true", help="Evaluate the seed and exit.")
    parser.add_argument("--fast-agent-bin", default="fast-agent")
    parser.add_argument("--run-name", default=None)
    return parser.parse_args()


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
    return rows


def next_candidate_dir(run_dir: Path) -> Path:
    index = 1
    while (run_dir / f"candidate-{index:03d}").exists():
        index += 1
    path = run_dir / f"candidate-{index:03d}"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run_batch(
    *,
    candidate: dict[str, str],
    candidate_dir: Path,
    model: str,
    fast_agent_bin: str,
) -> Path:
    instruction_path = candidate_dir / "instructions.md"
    output_path = candidate_dir / "results.jsonl"
    summary_path = candidate_dir / "summary.json"
    instruction_path.write_text(candidate["instructions"], encoding="utf-8")

    template = SMOKE_TEMPLATE if model == "passthrough" else TASK_TEMPLATE
    cmd = [
        fast_agent_bin,
        "--no-update-check",
        "--env",
        str(ENV_ROOT),
        "batch",
        "run",
        "--input",
        str(INPUT_PATH),
        "--output",
        str(output_path),
        "--instruction",
        str(instruction_path),
        "--template",
        str(template),
        "--schema",
        str(SCHEMA_PATH),
        "--model",
        model,
        "--id-field",
        "id",
        "--include-input",
        "--summary-output",
        str(summary_path),
        "--no-final-summary",
    ]
    proc = subprocess.run(cmd, cwd=ENV_ROOT.parent, text=True, capture_output=True, check=False)
    (candidate_dir / "batch.stdout.txt").write_text(proc.stdout, encoding="utf-8")
    (candidate_dir / "batch.stderr.txt").write_text(proc.stderr, encoding="utf-8")
    if proc.returncode:
        raise RuntimeError(f"fast-agent batch failed with exit {proc.returncode}\n{proc.stderr[-2000:]}")
    return output_path


def score_results(output_path: Path) -> tuple[float, dict[str, Any]]:
    rows = load_jsonl(output_path)
    failures: list[dict[str, Any]] = []
    ok = 0
    for row in rows:
        source = row.get("input") if isinstance(row.get("input"), dict) else {}
        result = row.get("result") if isinstance(row.get("result"), dict) else {}
        expected = source.get("expected")
        actual = result.get("category")
        if row.get("ok") is True and actual == expected:
            ok += 1
            continue
        failures.append(
            {
                "id": row.get("id") or source.get("id"),
                "expected": expected,
                "actual": actual,
                "request": source.get("text"),
                "error": row.get("error"),
            }
        )
    total = max(1, len(rows))
    score = ok / total
    side_info = {
        "scores": {"gepa_score": score, "accuracy": score, "rows": len(rows)},
        "failures": failures,
        "actionable_feedback": summarize_failures(failures),
    }
    return score, side_info


def summarize_failures(failures: list[dict[str, Any]]) -> list[str]:
    if not failures:
        return ["All rows passed. Keep the category boundaries concise and preserve the JSON schema."]
    hints: list[str] = []
    for failure in failures[:6]:
        hints.append(
            "Row {id}: expected {expected}, got {actual}. Request: {request}".format(
                id=failure.get("id"),
                expected=failure.get("expected"),
                actual=failure.get("actual"),
                request=failure.get("request"),
            )
        )
    return hints


def build_evaluator(run_dir: Path, *, model: str, fast_agent_bin: str):
    def evaluate(candidate: dict[str, str]) -> tuple[float, dict[str, Any]]:
        candidate_dir = next_candidate_dir(run_dir)
        (candidate_dir / "candidate.json").write_text(json.dumps(candidate, indent=2), encoding="utf-8")
        output_path = run_batch(
            candidate=candidate,
            candidate_dir=candidate_dir,
            model=model,
            fast_agent_bin=fast_agent_bin,
        )
        score, side_info = score_results(output_path)
        side_info["candidate_dir"] = str(candidate_dir)
        (candidate_dir / "score.json").write_text(json.dumps(side_info, indent=2), encoding="utf-8")
        print(f"{candidate_dir.name}: score={score:.3f}")
        return score, side_info

    return evaluate


def main() -> int:
    args = parse_args()
    run_name = args.run_name or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    run_dir = RUN_ROOT / run_name
    run_dir.mkdir(parents=True, exist_ok=True)

    seed = {"instructions": SEED_PATH.read_text(encoding="utf-8")}
    evaluator = build_evaluator(run_dir, model=args.model, fast_agent_bin=args.fast_agent_bin)

    if args.evaluate_only:
        score, side_info = evaluator(seed)
        print(json.dumps({"score": score, "run_dir": str(run_dir), **side_info}, indent=2))
        return 0

    if args.model == "passthrough":
        print("Use --evaluate-only for passthrough smoke tests, or pass --model with a real LLM.", file=sys.stderr)
        return 2

    try:
        from gepa.optimize_anything import (  # type: ignore[import-not-found]
            EngineConfig,
            GEPAConfig,
            ReflectionConfig,
            optimize_anything,
        )
    except ImportError:
        print('GEPA is not installed. Run: uv pip install "gepa"', file=sys.stderr)
        return 2

    result = optimize_anything(
        seed_candidate=seed,
        evaluator=evaluator,
        objective=(
            "Improve the support-request classification instruction. Preserve the JSON schema "
            "and make category boundaries explicit enough to generalize."
        ),
        config=GEPAConfig(
            engine=EngineConfig(max_metric_calls=args.max_metric_calls, cache_evaluation=True),
            reflection=ReflectionConfig(reflection_lm=args.reflection_lm),
        ),
    )
    best = result.best_candidate
    (run_dir / "best-instructions.md").write_text(best["instructions"], encoding="utf-8")
    (run_dir / "result.json").write_text(json.dumps({"best_candidate": best}, indent=2), encoding="utf-8")
    print(f"Best instructions written to {run_dir / 'best-instructions.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
