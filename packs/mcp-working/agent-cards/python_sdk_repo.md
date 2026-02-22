---
name: python_sdk_repo
tool_only: true
model: $system.code
shell: true
cwd: /home/shaun/source/mcp-work/working/repos/python-sdk
agents: [ripgrep_search]
function_tools:
  - python_sdk_multilspy_tools.py:lsp_hover
  - python_sdk_multilspy_tools.py:lsp_definition
  - python_sdk_multilspy_tools.py:lsp_references
  - python_sdk_multilspy_tools.py:lsp_document_symbols
  - python_sdk_multilspy_tools.py:lsp_workspace_symbols
  - python_sdk_multilspy_tools.py:lsp_diagnostics
---

You are focused on python-sdk.

{{file_silent:AGENTS.md}}

Priorities:
- Prototype examples/tests for SEP ideas.
- Prefer additive, experimental-flag style integration when possible.
- Minimize core SDK disruption unless necessary.
- Use LSP tools first for symbol-accurate code navigation.
