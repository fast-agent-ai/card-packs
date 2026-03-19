# hf-codemode

Production-style Hugging Face Hub codemode pack extracted from `.prod`.

Includes:

- `hub_search` — raw / fixed passthrough variant
- `hub_search_normal` — normal final-answer variant with selectable response mode
- `hub_search_selectable` — raw-runtime variant with selectable response mode, so callers can choose postprocessed output or passthrough per invocation
- `monty_api/` runtime package
- `fastagent.config.yaml` model reference defaults

## Required Python packages

Make sure these libraries are installed in the same Python environment that runs `fast-agent`.

If you use a project-local `.venv`:

```bash
source .venv/bin/activate
uv pip install pydantic-monty huggingface_hub
```

If you prefer not to activate the virtual environment first:

```bash
uv pip install --python .venv/bin/python pydantic-monty huggingface_hub
```

If `fast-agent` is running from some other environment, target that interpreter directly:

```bash
uv pip install --python "$(which python)" pydantic-monty huggingface_hub
```

## Install from marketplace

```bash
fast-agent cards add hf-codemode
```

## Notes

- The cards are read-only and intended for Hugging Face Hub discovery / lookup workflows.
- `hub_search` returns the raw runtime envelope directly.
- `hub_search_selectable` is the variant to use when a parent caller should be able to choose `response_mode: postprocess` or `response_mode: passthrough`.
- The included `fastagent.config.yaml` uses the same `$system.default` / `$system.raw` references as the source `.prod` setup.
