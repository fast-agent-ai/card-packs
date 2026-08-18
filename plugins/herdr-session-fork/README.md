# Herdr Session Fork

Adds `/fork-pane [title]` for branching an interactive `fast-agent go`
conversation into two Herdr panes.

The command uses fast-agent's normal session-fork behavior:

- The current pane activates and continues the new fork.
- A sibling Herdr pane opens the original session with `fast-agent go --resume`.
- A supplied title becomes the persisted title and Herdr display label of the fork.
- Without a supplied title, the fork keeps the current session title and the
  current Herdr pane keeps displaying it.

The command passes the active fast-agent workspace and home explicitly to the
new process, along with the current agent model. It confirms that Herdr detects
fast-agent in the sibling pane after launch and includes recent pane output in
the warning if startup cannot be confirmed.

```text
/fork-pane
/fork-pane Try the alternate implementation
/fork-pane "Review-only branch"
```

Herdr also receives each branch's session ID as a custom `session` metadata
token. A sidebar can show it with `$session`.

The command requires `HERDR_ENV=1`, an active persisted session, and the
`herdr` and `fast-agent` executables on `PATH`. It is intentionally available
only in the interactive TUI.

## Configuration

New panes choose `right` for wide panes and `down` for narrower panes. Set an
explicit direction to disable geometry-based selection:

```yaml
plugins:
  enabled:
    - herdr-session-fork
  config:
    herdr-session-fork:
      direction: down
```

The original session receives focus by default. Keep working in the current
fork instead, choose a split ratio, or tune startup confirmation with:

```yaml
plugins:
  config:
    herdr-session-fork:
      focus: fork
      ratio: 0.4
      startup_timeout_ms: 10000
```

Valid `focus` values are `original` and `fork`. Ratios from `0.1` through `0.9`
are passed to Herdr. Set `startup_timeout_ms: 0` to skip launch confirmation;
the maximum is 30000.

If pane creation or launch fails after the session was forked, the command
leaves the current pane on the valid fork and prints a command for manually
resuming the original session.
