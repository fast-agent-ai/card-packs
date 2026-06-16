# gepa-demo

Starter files for a small GEPA loop driven by fast-agent batch runs.

Install and open the helper agent:

```bash
fast-agent go --pack gepa-demo
```

Smoke-test the evaluator without calling an external model:

```bash
uv run .fast-agent/scripts/gepa-run.py --evaluate-only
```

Run a real optimization after installing GEPA:

```bash
uv pip install "gepa"
uv run .fast-agent/scripts/gepa-run.py \
  --model "responses.gpt-5.4-mini" \
  --reflection-lm "openai/gpt-5" \
  --max-metric-calls 12
```

The demo optimizes `.fast-agent/seed/instructions.md` against
`.fast-agent/data/input.jsonl`. Each candidate is evaluated with
`fast-agent batch run`, scored by `scripts/gepa-run.py`, and written under
`.fast-agent/gepa-runs/`.

The helper AgentCard can explain the files and point you back to the
fast-agent GEPA guide.
