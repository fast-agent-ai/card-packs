# Herdr Session Fork

Adds `/fork-pane [title]` for branching an interactive `fast-agent go`
conversation into two Herdr panes.

The command uses fast-agent's normal session-fork behavior:

- The current pane activates and continues the new fork.
- A sibling Herdr pane opens the original session with `fast-agent go --resume`.
- A supplied title becomes the persisted title of the fork (and renames the
  pane when running locally in `herdr-app`).
- Without a supplied title, the fork keeps the current session title.

The command passes the active fast-agent workspace and home explicitly to the
new process, along with the current agent model. It confirms that `fast-agent`
starts in the sibling terminal via `hc get` and includes recent terminal output
(`hc read`) in the warning if startup cannot be confirmed.

```text
/fork-pane
/fork-pane Try the alternate implementation
/fork-pane "Review-only branch"
```

Each branch's terminal receives a `session=<session-id>` label via `hc label`.

## Local and Remote Machines

- **Local (`herdr-app` + `hc`):** splits the current pane directly with
  `herdr-app do pane.split`, letting Herdr manage pane proportions
  automatically. Supports `direction`, `focus`, and pane renaming.
- **Remote SSH machines (`hc` only):** launches the resumed branch with
  `hc run --bg --no-parent --keep` carrying the current terminal's
  `herdr.space` and `herdr.tab` labels. Desktop `herdr-app` automatically
  adopts the remote terminal into a side-by-side sibling pane in the same tab.

The command requires `HC_TERMINAL_ID` and `hc` on `PATH` (plus `herdr-app` for
local pane splitting/focus). It is intentionally available only in the
interactive TUI.

## Configuration

New local panes choose `right` for wide terminals (`cols >= rows * 2`) and
`down` for narrower ones. Set an explicit direction to disable geometry-based
selection:

```yaml
plugins:
  enabled:
    - herdr-session-fork
  config:
    herdr-session-fork:
      direction: down
```

The original session receives focus by default on local panes. Keep working in
the current fork instead, or tune startup confirmation with:

```yaml
plugins:
  config:
    herdr-session-fork:
      focus: fork
      startup_timeout_ms: 10000
```

Valid `focus` values are `original` and `fork`. Set `startup_timeout_ms: 0` to
skip launch confirmation; the maximum is `30000`.

If pane creation or launch fails after the session was forked, the command
leaves the current pane on the valid fork and prints a command for manually
resuming the original session.
