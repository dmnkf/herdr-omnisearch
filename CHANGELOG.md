# Changelog

## 0.11.2 - 2026-09-25

- Open the pickers faster. The `open-live` and `open-archive` actions now call
  `herdr plugin pane open` directly instead of starting Python only to send
  that request, which saves 40 to 75 ms on every open.

## 0.11.1 - 2026-09-25

- Fix the live index growing without bound. The fuzzy-search vocabulary kept
  every word that ever appeared in a pane, so busy machines reached hundreds
  of megabytes to over a gigabyte for a few dozen panes. Searches slowed to
  about 100 ms per keystroke, and every refresh rewrote a huge file. Words no
  pane contains any more are now forgotten after each index run.
- An index that is already bloated, or still carries the archive tables
  removed in 0.7.0, is rebuilt from the panes on the next run. That takes about
  a second and brings it back to a few megabytes. Free pages are vacuumed once
  they make up a quarter of the file. The archive catalog is not affected.

## 0.11.0 - 2026-09-25

- Restyle both pickers after Herdr's session navigator. Rows use Herdr's
  status dots (`●` working, blocked and done, `○` idle, `·` unknown) in its
  colours instead of bracketed states, and machines, workspaces and panes form
  a `▾` / `├──` tree. Paths and counts sit quietly on the right, and the
  selection is a single accent bar.
- The search line reads ` / query`, with a placeholder when empty and the
  result count on the right. A two-line footer shows the selected row and the
  key hints. The mode banner and help line are gone, and matching text still
  shows below the list.
- The popup fills the pane area. Herdr sizes plugin popups within the panes,
  so unlike its own navigator it cannot cover the sidebar.

## 0.10.1 - 2026-09-25

- Open the live and archive pickers as a centered popup (80% of the screen),
  like Herdr's own session navigator, instead of a zoomed overlay that takes
  over the active pane. Popups need Herdr 0.7.4, which the plugin's minimum
  version already covers.

## 0.10.0 - 2026-09-25

- Type what you are looking for. In live search, a word that starts the name
  of a machine or an agent, or names a status (`working`, `blocked`, `idle`,
  `done`), also counts as that filter. So `billing work` finds billing panes
  on the workbox machine, and `codex blocked` finds blocked Codex agents on
  every machine, even though those words do not appear in the panes.
- Such a word still matches as text too (the exact word), and rows matching
  the filter rank first. The filters apply inside the SQL search, so results
  stay complete however large the index grows.
- The picker shows how it read the query next to what you type, for example
  `→ workbox (machine) · text: billing`.
- `@name` and `#name` filter by machine and workspace when they name one, and
  otherwise stay text, so `#3845` or `@pytest.fixture` search as typed. The
  `machine:`, `agent:`, `status:`, `workspace:` and `cwd:` filters accept `m:`,
  `a:`, `s:`, `w:` and `c:`.
- Compound names are indexed with their parts, so `api` finds `api-server`.
  Queries keep whole words, so typo tolerance for compound words is unchanged.
- With `--local-only`, words are never read as machine names.

## 0.9.1 - 2026-09-25

- Read OpenCode history only from its SQLite database (OpenCode 1.2 and
  newer), and drop the reader for the older JSON storage and its `storage`
  option. To upgrade an OpenCode older than 1.2, run a 1.2.x release once
  (`opencode db migrate`), because later releases no longer migrate the old
  files.

## 0.9.0 - 2026-09-25

- Search OpenCode conversations in ArchiveSearch, next to Codex and Claude
  Code. OmniSearch lists top-level sessions read-only from OpenCode's storage
  (its SQLite database, or the JSON files of older releases) and reads each
  changed session with `opencode export`. Subagent sessions are skipped;
  archived sessions are included. Resume runs `opencode --session <id>` in
  the session's directory.
- Exports are written to a temporary file, because OpenCode cuts off output
  sent to a pipe at 64 KiB.
- A session whose export fails keeps its previous catalog entry and is
  retried on the next refresh. An unreadable OpenCode database leaves
  existing entries in place. One run stops exporting once `opencode` turns out
  to be missing or keeps timing out, and spends at most ten minutes on
  OpenCode, so it cannot block Codex and Claude refreshes.
- `opencode` is found on PATH or in its usual install locations (`~/.opencode`,
  `~/.bun`, Homebrew, npm global). `doctor` shows which binary is used, and
  `[archive.opencode]` accepts `database`, `storage` and `export` overrides.
- The default `agents` list is now `codex, claude, opencode`. Configs that set
  `agents` explicitly need `opencode` added.

## 0.8.1 - 2026-09-25

- Stop long row titles from running into the path column. Titles now end in
  `…` before the column, and long paths keep their final directories, where
  worktree names usually are.
- Pin top matches only when hits span more than one machine. Previously a
  query matching a single machine showed every hit twice.
- Pane rows under a workspace header no longer repeat the workspace name. The
  preview header still shows the full path. This also applies to
  single-machine setups.

## 0.8.0 - 2026-09-25

- Search every machine at once. When Herdr has saved SSH machines
  (`herdr machine add`), OmniSearch pulls each machine's live index over SSH
  and merges it into the picker. The tree gains a machine level above
  workspaces, and a query pins the five best hits across all machines above
  the tree. Each machine gets its own result limit, so a busy host cannot
  crowd out the others.
- Selecting a result on another machine focuses that exact pane on its server
  and shows a toast. Herdr does not let other processes switch the client's
  selected machine, so select it in the sidebar or with `prefix+w` to land
  there.
- Every machine keeps indexing its own panes with its own config. The merging
  machine reads each remote's `export`, which returns that session's own rows
  and never re-exports synced ones. It refreshes the remote index first when
  the remote watcher is not running. Remotes need OmniSearch 0.8.0 and
  `python3`. The SSH command is a single line, so any login shell works,
  csh included.
- New commands: `export`, `sync-machines`, `machines`. There is a new
  `sync-machines` plugin action and a `machine:` query filter. `--local-only`
  on `search` and `pick` restores single-machine results. `doctor` reports
  per-machine sync state.
- New `[machines]` config section: `enabled`, `exclude`, `sync_seconds`,
  `ssh`, `connect_timeout_seconds`, `timeout_seconds`. The watcher refreshes
  machines every `sync_seconds` in the background. A machine that cannot be
  reached keeps its last rows and is marked offline. A remote that cannot read
  its own Herdr server is marked stale. A malformed export fails only its own
  machine, and a failing `herdr machine list` never drops synced rows.
- Single-machine setups are unchanged. Search results and the picker stay
  exactly as they were. The picker does no machine work, and the watcher checks
  for saved machines at most every five minutes. `enabled = false` turns that
  off.
- Fix live tree grouping for panes from different sessions that share a
  workspace id.

## 0.7.0 - 2026-09-20

- Remove the legacy `archive-index` window path: its command, SQLite tables,
  `max_files`/`since_days` config keys and doctor fields. Archive search is
  the catalog only. Existing databases keep their old tables untouched.
- Split the single `cli.py` into layered modules (settings, storage,
  textmatch, live_index, archive_catalog, render, navigate, picker, watcher,
  cli). No command or flag changed apart from the removed legacy ones.
- Tests default `HERDR_PLUGIN_STATE_DIR` to a temporary directory so a plain
  test run can no longer purge the real plugin state.

## 0.6.11 - 2026-09-20

- Read live panes in `ansi` format over the socket and drop `herdr agent read`,
  so indexing no longer scrolls idle agent panes to harvest alternate-screen
  history. Full index sweeps drop from ~20s to well under a second; live text
  from full-screen agents shrinks to the retained rows. (#2 by @mkpoli, fixes
  #1 and #3)

## 0.6.10 - 2026-09-20

- Hide Codex subagent sessions, such as guardian approval reviews, from
  archive search using the session metadata instead of prompt text.

## 0.6.9 - 2026-08-31

- Search persisted Herdr workspace names with normal archive queries and rank
  workspace matches ahead of conversation-only matches.
- Refresh workspace renames without reparsing unchanged session histories.

## 0.6.8 - 2026-08-22

- Center live previews on the latest matching terminal line so agent replies
  remain visible when prompts contain the same search term.

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
