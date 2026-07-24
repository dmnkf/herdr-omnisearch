# herdr-omnisearch

Fast local search and navigation for Herdr workspaces, panes, agent chats, and
archived agent session logs. The name and the instant, typo-tolerant,
just-works search feel are inspired by
[Omnisearch for Obsidian](https://github.com/scambier/obsidian-omnisearch) —
this is that idea applied to a terminal session instead of a vault.

OmniSearch is a Herdr 0.7.5 plugin. It uses the stable `herdr agent` CLI for
live agent identity, terminal reads, focus, and validated starts. The Herdr
socket remains responsible for workspace topology, ordinary shell panes,
plugin panes, and event subscriptions. SQLite FTS5 remains local and owns both
live search data and archived-session search.

It keeps a private SQLite FTS5 index in Herdr's plugin state directory:

```text
~/.local/state/herdr/plugins/herdr.omnisearch/index.sqlite3
```

Standalone CLI installs without the plugin use
`~/.local/share/herdr-omnisearch/index.sqlite3` instead; a legacy index at that
path is migrated into the plugin state directory on first plugin use.

The CLI is installed as:

```bash
herdr-omnisearch
```

## Install

Install the public plugin directly from GitHub:

```bash
herdr plugin install dmnkf/herdr-omnisearch
```

Then verify the installation:

```bash
herdr plugin action invoke doctor --plugin herdr.omnisearch
```

To pin this release:

```bash
herdr plugin install dmnkf/herdr-omnisearch --ref v0.3.6
```

Herdr plugin manifests do not modify user keybindings. The portable recommended
bindings are:

```toml
[[keys.command]]
key = "prefix+o"
type = "plugin_action"
command = "herdr.omnisearch.open-live"
description = "OmniSearch"

[[keys.command]]
key = "prefix+shift+o"
type = "plugin_action"
command = "herdr.omnisearch.open-archive"
description = "ArchiveSearch"
```

Add that block to `~/.config/herdr/config.toml`, then run
`herdr server reload-config`. On macOS, `cmd+o` and `cmd+shift+o` can be used
instead when the terminal forwards those chords to Herdr.

For a local checkout:

```bash
./install.sh
```

The default install is offline-friendly. It links this checkout as
`herdr.omnisearch`, creates a `~/.local/bin/herdr-omnisearch` wrapper, atomically
moves the existing local index into Herdr's plugin state directory on first use,
starts the event watcher, and installs plugin-action bindings for the live and
archive pickers. The local installer defaults to `prefix+o` and
`prefix+shift+o`.

See [INSTALL.md](INSTALL.md) for the full second-VM flow and available flags.

For a traditional editable Python install from this checkout:

```bash
python3 -m pip install --user -e .
```

This checkout intentionally uses `setup.cfg` plus `setup.py` so editable installs work in offline environments with older system setuptools.

Equivalent direct setuptools install:

```bash
python3 setup.py develop --user
```

With uv:

```bash
uv pip install --system -e .
```

After installing, check the target VM:

```bash
herdr-omnisearch doctor
```

## Commands

Open managed plugin panes:

```bash
herdr plugin pane open --plugin herdr.omnisearch --entrypoint live
herdr plugin pane open --plugin herdr.omnisearch --entrypoint archive
```

Index live Herdr panes:

```bash
herdr-omnisearch index --lines 350
```

Search live workspaces and chats:

```bash
herdr-omnisearch search deploy notes
```

Open the live picker:

```bash
herdr-omnisearch pick --no-refresh --background-refresh --stale-seconds 10 --lines 350
```

Native picker controls are Vim-style:

```text
insert mode:
  type          search
  Backspace     delete
  Ctrl-u        clear search
  Esc           normal mode
  Enter         focus selected row

normal mode:
  j / k         next / previous row
  Ctrl-d/u      page down / page up
  gg / G        first / last row
  i or /        insert mode
  c             clear search and insert
  a or :        action palette for selected row
  Enter         focus selected row
  q or Esc      quit

action mode:
  type          filter actions
  j / k         next / previous action
  Enter         run action
  Esc           normal mode
```

Current actions include exact focus, rename workspace, rename pane, and yanking cwd/session/pane/workspace ids.

Index archived agent session logs:

```bash
herdr-omnisearch archive-index
```

Search archived sessions:

```bash
herdr-omnisearch archive-search migration notes
```

Open the archive picker:

```bash
herdr-omnisearch archive-pick --no-refresh --background-refresh --stale-seconds 3600
```

Check state:

```bash
herdr-omnisearch doctor
```

Manage the event-driven live index watcher:

```bash
herdr-omnisearch watch-status
herdr-omnisearch watch-start
herdr-omnisearch watch-stop
```

## Environment

Plugin invocations use the Herdr-provided `HERDR_PLUGIN_CONFIG_DIR` and
`HERDR_PLUGIN_STATE_DIR`. Direct CLI invocations remain backward compatible
with the legacy config below, but automatically reuse Herdr's installed plugin
config and state directories when they exist. Direct and plugin commands
therefore report the same database and watcher:

```text
~/.config/herdr-omnisearch/config.ini
```

Use `config.example.ini` as a starting point.

`HERDR_OMNISEARCH_CONFIG` overrides the config file path.

`HERDR_BIN` overrides the Herdr binary path. The portable default is `herdr` from `PATH`.

`HERDR_OMNISEARCH_DB` overrides the SQLite database path.

`HERDR_OMNISEARCH_COMMAND` overrides the command used for background indexing and fzf previews. Normally this is discovered automatically from the installed `herdr-omnisearch` command.

`HERDR_SOCKET_PATH` selects the Herdr socket. Named sessions can instead use
`HERDR_SESSION`.

## Multiple sessions

Concurrent Herdr sessions on one machine share the SQLite database but own
disjoint index rows: each session's indexer and watcher only replace rows
belonging to that session, so sessions never overwrite each other. Search and
the picker show the current session by default; pass `--all-sessions` to
`search` or `pick` to include every session. Selecting a result from another
session focuses it through that session's socket. `doctor` prints the derived
`herdr_session` key.

## Config

Archive indexing is disabled by default. Enable it only if you want local agent
conversation histories copied into the OmniSearch SQLite index. Start from the
example returned by `herdr plugin config-dir herdr.omnisearch`:

```ini
[herdr]
bin = herdr
fallback_cwd = ~

[archive]
enabled = false
max_files = 500
since_days = 90
agents = codex, claude

[archive.codex]
sessions = ~/.codex/sessions/**/*.jsonl
thread_names = ~/.codex/session_index.jsonl
resume = codex resume -C "{cwd}" {session_id}
launcher = agent
kind = codex
start_timeout_ms = 60000

[archive.claude]
sessions = ~/.claude/projects/*/*.jsonl
resume = claude --resume {session_id}
launcher = agent
kind = claude
start_timeout_ms = 60000

[skip]
pane_label_contains = omnisearch
unknown_without_agent = true
workspace_cwd_pairs =

[workspace_labels]
strip_prefixes =
worktree_markers = worktrees
remove_words =

[workspace_labels.exact]
# /path/to/project = Friendly Space Name
```

Set `enabled = true` to opt in. `max_files` and `since_days` bound archive
growth; set either to `0` only when an unbounded archive is intentional.

`launcher = agent` validates resumed sessions through `herdr agent start` and
is the portable default. Use `launcher = shell` when the configured resume
command must pass through an interactive shell function or wrapper. Shell mode
preserves the complete `resume` command; all later identity, reads, and focus
operations still use Herdr's agent automation interface after detection.

## Privacy and local data

OmniSearch runs as your user and stores indexed terminal output, workspace
metadata, paths, and optionally agent conversation histories in a local SQLite
database. Indexed content may contain source code, credentials, private
messages, or other sensitive text that appeared in a pane or session log.

- The index stays under Herdr's plugin state directory and is not uploaded.
- The database and state directory are created with user-only permissions where
  the platform allows it.
- Archive-history indexing is disabled by default.
- Review `config.ini` before enabling archive indexing or adding custom paths.
- Do not synchronize or publish the plugin state directory.

Inspect index size and status:

```bash
herdr-omnisearch doctor
```

Stop the watcher and permanently remove the local index:

```bash
herdr-omnisearch purge --yes
```

## Update and uninstall

Herdr v1 plugins update by reinstalling the GitHub source:

```bash
herdr plugin install dmnkf/herdr-omnisearch
```

Unregister the plugin:

```bash
herdr plugin uninstall herdr.omnisearch
```

Plugin configuration and state are user-owned. Use `herdr plugin config-dir
herdr.omnisearch` and `herdr-omnisearch doctor` to locate them before removing
those directories manually.

## Notes

The live index intentionally skips unmapped `unknown` panes by default. Use `--include-wrappers` when debugging Herdr pane metadata.

Selecting an archived session is an explicit focus operation. When a new space
is needed, OmniSearch creates one workspace and starts the resume command in its
root pane; it does not create a second wrapper pane.

Workspace rows render as space headers, and agent rows render indented beneath them:

```text
[workspace] project alpha
  [working] project alpha / agent / service shell
```
