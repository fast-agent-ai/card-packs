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
new process, along with the current agent model. It creates and focuses the new
pane before launching the resumed session there.

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

New panes split to the right by default. Set `direction` to `down` if preferred:

```yaml
plugins:
  enabled:
    - herdr-session-fork
  config:
    herdr-session-fork:
      direction: down
```

If pane creation or launch fails after the session was forked, the command
leaves the current pane on the valid fork and prints a command for manually
resuming the original session.
