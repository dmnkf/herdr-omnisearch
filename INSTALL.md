# Installation

The supported public installation path is:

```bash
herdr plugin install dmnkf/herdr-omnisearch
```

Use `--ref v0.4.1` to pin this release. The plugin has no runtime
dependency outside Python 3.9+ and the `herdr` command.

GitHub plugin installation does not modify `~/.config/herdr/config.toml`. Add
the portable default bindings explicitly:

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

Reload them with `herdr server reload-config`.

## Local or offline installation

For development or an offline target, copy a checkout to the target VM.

## Copy

Copy this whole directory to the target VM, for example:

```bash
scp -r herdr-omnisearch user@vm:~/herdr-omnisearch
```

Or build a clean copy-over tarball:

```bash
scripts/make-bundle.sh
scp dist/herdr-omnisearch-portable.tar.gz user@vm:~
```

Then unpack it on the target VM:

```bash
tar -xzf ~/herdr-omnisearch-portable.tar.gz
```

On the target VM:

```bash
cd ~/herdr-omnisearch
./install.sh
```

The default installer:

- writes `~/.local/bin/herdr-omnisearch`
- creates `~/.config/herdr-omnisearch/config.ini` if it does not exist
- links the checkout as the `herdr.omnisearch` plugin
- seeds Herdr's plugin config directory from the existing config
- starts the socket event watcher
- installs `plugin_action` keybindings in `~/.config/herdr/config.toml`

It does not copy indexes or session data from another VM. On an existing
installation, the first plugin invocation atomically moves the legacy SQLite
database into Herdr's plugin state directory and leaves compatibility links at
the old path. A new VM builds its own index.

## Activate

Make sure `~/.local/bin` is on `PATH`, then check the install:

```bash
herdr-omnisearch doctor
```

The installer reloads Herdr config. The plugin can be inspected directly:

```bash
herdr plugin list --plugin herdr.omnisearch
herdr plugin action list --plugin herdr.omnisearch
herdr-omnisearch watch-status
```

The default keybindings are:

```text
prefix+o        live OmniSearch picker
prefix+shift+o  archive search picker
```

To use direct macOS chords instead:

```bash
./install.sh --live-key cmd+o --archive-key cmd+shift+o
```

## Offline install

`./install.sh` uses wrapper mode by default. That mode does not need pip or
network access. The wrapper points at the copied checkout, so keep the checkout
in place after installing.

For an editable Python package install instead:

```bash
./install.sh --mode pip
```

## Existing config

The installer will not replace an existing OmniSearch config unless asked:

```bash
./install.sh --force-config
```

To avoid touching Herdr keybindings:

```bash
./install.sh --no-keybindings
```

To install only the standalone CLI without linking a plugin:

```bash
./install.sh --no-plugin
```

To link the plugin without starting the live watcher:

```bash
./install.sh --no-watcher
```

To preview without writing anything:

```bash
./install.sh --dry-run
```

## Paths

Useful overrides:

```bash
./install.sh --bin-dir ~/.local/bin
./install.sh --config ~/.config/herdr-omnisearch/config.ini
./install.sh --herdr-config ~/.config/herdr/config.toml
./install.sh --herdr-bin /path/to/herdr
```

The OmniSearch config itself can also be selected at runtime with:

```bash
HERDR_OMNISEARCH_CONFIG=/path/to/config.ini herdr-omnisearch doctor
```

## What to copy if you want the same archive scope

Archive search reads local agent history from the configured paths. The defaults
are:

```text
~/.codex/sessions/**/*.jsonl
~/.codex/session_index.jsonl
~/.claude/projects/*/*.jsonl
```

On a second VM, only copy those histories if you explicitly want that VM to
search the same archived chats. Otherwise leave them local to each machine.
