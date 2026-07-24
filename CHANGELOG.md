# Changelog

## 0.4.0 - 2026-07-24

- Scope the live index, watcher, and background indexing per Herdr session so
  concurrent sessions on one machine no longer overwrite each other's rows.
- Filter search and the pickers to the current session by default; add
  `--all-sessions` and route cross-session focus and rename through the
  originating session's socket.
- Stop legacy pre-session watchers on upgrade before they can rebuild the
  shared table.

## 0.3.6 - 2026-07-23

- Parse watcher lock metadata correctly when reporting or stopping its process.
- Use the kernel-owned watcher lock as the health-check liveness source.

## 0.3.5 - 2026-07-23

- Use prefix-first live and archive keybindings as the portable installer defaults.
- Add explicit installer overrides for direct macOS command-key bindings.
- Document the manual keybinding step required after a managed GitHub install.

## 0.3.4 - 2026-07-23

- Serialize the first-start database migration with a file lock and repair
  self-referential index symlinks left behind by interrupted migrations.
- Replace stale-lock heuristics for background indexing and the watcher with
  kernel-owned locks that release automatically when their process exits.
- Run CI on macOS in addition to Linux.

## 0.3.3 - 2026-07-23

- Force managed live and archive panes to use the native interactive picker.
- Avoid immediate overlay exit when terminal capability detection selects noninteractive mode.

## 0.3.2 - 2026-07-23

- Create missing plugin-state parent directories before opening SQLite.
- Repair private directory, database, and SQLite sidecar permissions before use.
- Report broken links and invalid database paths with actionable diagnostics.
- Preserve existing database content during state repair.

## 0.3.1 - 2026-07-23

- Initial public release.
- Search and navigate live Herdr workspaces, panes, and archived sessions.
- Provide a native terminal picker, event-driven refresh, and health diagnostics.
- Use the Herdr 0.7.5 agent CLI for canonical identity, reads, focus, and starts.
- Preserve socket-based topology, shell-pane access, plugin panes, and events.
- Add explicit native and shell archive launchers for wrapper compatibility.
- Correct archive matching when process detection and session providers differ.
- Keep background jobs pinned to the active Herdr-managed plugin checkout.
