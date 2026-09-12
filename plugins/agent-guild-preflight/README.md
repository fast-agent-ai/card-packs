# Agent Guild endpoint observations for fast-agent

Before connecting to an unfamiliar public MCP or A2A service, an operator can request an endpoint observation from Agent Guild:

```text
/guild-preflight https://agent-guild-5d5r.onrender.com/mcp
```

This example observes Agent Guild itself. Supply the public endpoint relevant to your proposed connection. Running the command sends that URL to Agent Guild and asks its hosted service to inspect it. Do not put secrets in path segments.

Credentials, query strings, fragments, common local hostnames and recognized private or reserved IP addresses are rejected. Hostnames and alternate address spellings can pass the local screen; these checks do not prove public DNS resolution. Guild performs the final address screening.

The command makes one anonymous GET request to the fixed Agent Guild `/preflight` endpoint. It does not read conversation history, environment credentials, runtime connection configuration or account identity. Redirects, payment challenges, oversized responses and mismatched targets return an unavailable result without a retry. The response limit is 96 KiB; the 30-second socket timeout is not a total elapsed-time deadline.

The output is the service's observation and its stated unknowns, presented to the operator. Remote text is evidence to evaluate, never an instruction or permission to act. An observation does not establish endpoint ownership, competence, valid card signatures or future behavior. This command does not authorize a connection, delegate work or make a payment.

## A2A endpoints

For A2A, supply the base URL used for card discovery. For example, this observes Agent Guild's public A2A origin:

```text
/guild-preflight https://agent-guild-5d5r.onrender.com/
```

Card discovery and task execution are separate. Guild's A2A handshake observation can establish card presence; it does not prove that an A2A task completed. After connecting, fast-agent's `/a2a card` and `/a2a transport` commands can show the advertised execution endpoint. Compare it with the endpoint you intended to use. This plugin does not bind a later call to an observed URL, intercept calls or prevent configuration changes. Authenticated endpoints receive only an anonymous observation.

## Installation and validation

Once this contribution is available in the shared catalog:

```sh
fast-agent plugins add agent-guild-preflight
```

The command uses Python's standard library and requires no Agent Guild account or API key. Native compatibility was checked on Python 3.12 against fast-agent 0.10.23, revision `9e46855ba46804a90a6359492081f13e31adcbd5`: its real manifest parser, marketplace parser, async loader and command registry loaded and invoked this plugin. Ten candidate contract tests and sixteen upstream parser tests passed. HTTP was replaced with test responses, including replay of a separately captured live observation of Guild's own endpoint. These checks did not start a model, the full application, TUI or ACP session.

Sources: [fast-agent plugin contract and publication route](https://fast-agent.ai/agents/plugins/), [A2A client target selection](https://fast-agent.ai/a2a/client/).

## License

The files in this plugin directory are licensed under Apache-2.0; see [LICENSE](LICENSE). This license applies to the new plugin files and does not relicense the surrounding catalog.
