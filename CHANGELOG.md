# Changelog

## 0.6.7 - 2026-08-12

- Keep stale catalog refreshes reliable when archive panes are force-closed.

## 0.6.6 - 2026-08-12

- Render archive input immediately and search once after a short typing pause.
- Keep the first two typed characters index-free, then use bounded exact/prefix search.
- Defer catalog refreshes until the picker closes and bound result enrichment.

## 0.6.5 - 2026-08-07

- Ignore inactive exact vocabulary when falling back to typo-tolerant search.

## 0.6.4 - 2026-08-07

- Exclude remaining task, goal, reasoning, shell, skill, and routing envelopes.

## 0.6.3 - 2026-08-07

- Exclude structured approval decisions from conversational content.
- Hide sessions without a substantive latest-turn preview from empty browsing.

## 0.6.2 - 2026-08-07

- Exclude legacy tool, shell, skill, and approval wrappers from content search.
- Prefer substantive chat turns over reconnect greetings in session previews.

## 0.6.1 - 2026-08-07

- Keep archive chat summaries clear of the fixed cwd display column.
- Exclude interruption and local-command control messages from chat previews.

## 0.6.0 - 2026-08-07

- Index bounded user and assistant turns instead of session metadata token bags.
- Show matching chat context and latest-turn previews in archive results.
- Keep exact and typo-tolerant content search fast with one disk-backed FTS index.

## 0.5.0 - 2026-08-06

- Add a persistent, incremental archive catalog with bounded per-file memory.
- Search all archive dates without rebuilding history windows during typing.
- Make 14-day browsing instant and prioritize title matches over incidental paths.

## 0.4.6 - 2026-08-05

- Return cross-window title and session metadata matches immediately instead
  of blocking on a full 14-day content-index rebuild.
- Resume metadata results directly and suppress the misleading `No matches`
  line while a search is still scanning.

## 0.4.5 - 2026-07-30

- Continue cross-window metadata ranking when the active window contains only
  a lower-quality space-label or path match.

## 0.4.4 - 2026-07-30

- Assign archive windows by session creation time instead of mutable file
  modification time.
- Prefer title and session-id matches across all windows over newer incidental
  path matches.

## 0.4.3 - 2026-07-30

- Continue archive searches through older 14-day windows when the active
  window has no direct match, using metadata first and content only as needed.
- Bound individual archive records before decoding so unusually large history
  entries cannot cause large indexing memory spikes.

## 0.4.2 - 2026-07-30

- Keep only one calendar-aligned archive window in the index, defaulting to the
  newest 14 days, with left/right picker navigation to older/newer windows.
- Filter source files before parsing and stream archive rows and token metadata
  into SQLite so index memory stays bounded.

## 0.4.1 - 2026-07-24

- Reap index rows, staleness markers, and watcher state of sessions whose
  Herdr socket no longer accepts connections.
- Make purge stop every session's watcher and remove all lock and log state.
- Tolerate WAL sidecar files vanishing while concurrent processes checkpoint.

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
