# mcp-working

Cross-repository MCP engineering workspace for the specification, transports
working group, Python SDK, and TypeScript SDK.

The pack expects this layout relative to the fast-agent working directory:

```text
working/
├── repos/
│   ├── modelcontextprotocol/
│   ├── transports-wg/
│   ├── python-sdk/
│   └── typescript-sdk/
└── scripts/
```

The Python and TypeScript SDK agents use `multilspy`; install it in the Python
environment that runs fast-agent. Their language servers must also be
available in that environment/workspace.

The required `$system.fast` and `$system.code` model references must be
configured before use.
