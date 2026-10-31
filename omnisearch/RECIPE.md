# OmniSearch

Ctrl+Shift+F, then type: find any terminal by what it shows or what its agent was told, on this computer and every connected machine. Tab switches to your old Claude Code, Codex and other agent sessions, and Enter resumes one where it ran. The searching is done by the `herdr-omnisearch` command on each machine. This mod is the popup in front of it.

This file is the brief: enough to build OmniSearch from an empty folder. The files next to it are one build of it. If they don't work on your Herdr, or you want it different, build it again from here.

## What you see

- A popup near the top of your window, 720px wide and up to 80% of its height, offset 40px from the top. Nothing is added to the layout itself.
- A heading row: "OmniSearch" (15px, semibold), then two chips, "Live" and "Sessions" (12px, the current one on the `selected` color). A small spinner shows on the right while any machine is still answering, and the hint "Tab live/sessions · Esc" sits at the far right (11px, `dim`).
- A search field 36px high with a 9px radius, a 1px `accent` border on the terminal `background`, and the shipped search icon. The placeholder reads "Search terminals by what they show or said…".
- A list of up to 8 rows at a time, each 46px with a 7px radius:
  - Line 1: a status glyph, the title (13px, `foreground`, semibold when selected) and tags at the right (11px, `dim`).
  - Line 2: the matching text (11.5px, `muted`), cut to one line.
  - The selected row is on `selected` and hovered rows on `hover`.
- **Live rows** are terminals. Their title is the agent's own title, else the terminal's, else the Claude Code session name. The tags are the agent, the space and, for another machine, the machine's name.
- A space that matched by name but has no matching terminal gets its own row, with "space" as its second line.
- **Sessions rows** are old agent sessions. Their tags are the agent, the space name, and how long ago the session last changed ("5m", "3h", "2d"), or "running" when a terminal is running it now.
- With no rows, one line of `dim` text explains why. It reads "Searching…", "Nothing matches "…"", "No terminals to show yet." or a machine's error. With no machine that has `herdr-omnisearch`, it gives the install command.

## How it behaves

- Status glyphs come from the terminal's agent:
  - Working is the shipped spinner, turning once every 900 ms in `working`.
  - Blocked is "●" in `blocked`, done is "●" in `done`, and idle is "○" in `idle`.
  - Anything unknown is "·" in `dim`.
  - A session row shows "↺", or "●" in `working` when it is running now.
- Opening the popup searches at once with an empty query. The live list then shows every agent terminal, the busiest first, from what each machine indexed last.
- At the same time each machine re-reads herdr-core's terminals with `app-search --refresh --limit 0`. It also starts a background refresh of its session catalog when that is over a minute old. As each machine finishes, its own rows are searched again. A refresh that failed says why, under the list or in place of it.
- Typing searches again 90 ms after the last key. Each machine shows the answer to its newest search only, so a slow answer to older words is dropped. Each machine's rows replace its old ones as they arrive, so the list never empties while you type.
- Up and Down move the selection, stopping at the ends; Page Up and Page Down move 8 rows. The list scrolls only as far as it takes to keep the selection in view. The wheel scrolls it too.
- Tab switches between Live and Sessions and searches the same words there. Clicking a chip does the same.
- Enter, or a click, acts on the row and closes the popup:
  - **A terminal with a pane:** `tab.select` on its tab, then `pane.focus` on the terminal.
  - **A terminal without a pane:** `agent.focus` for a reporting agent, `terminal.peek` for anything else.
  - **A space:** `space.select`.
  - **A session running now:** focuses its terminal, as above.
  - **Any other session:** `tab.new` with its resume command, in the space already on its folder on that machine. Without such a space, `space.new` on that machine.
  - **A session with no resume command:** a warning toast instead.
- Esc closes the popup.

## Built from

- **One mod**, `mods/omnisearch/`, with `main.js` and `mod.json`. The manifest has `place: "none"` and a popup overlay at the top, so no layout changes.
- **A `keys.json` snippet**, merged into the person's keys. It binds `ctrl-shift-f` to `mod.open:omnisearch` in both `global` and `terminal`, so it also opens while a terminal has focus.
- **The program it runs:**
  - `exec("herdr-omnisearch", ["app-search", "--limit", "40", ...flags, "--", ...words], { on })` once per machine, with `on` null for this computer and each name from `machines()` whose layout isn't `unavailable`.
  - The flag `--archive` searches Sessions. `["app-search", "--refresh", "--limit", "0"]` is the refresh, sent once per machine on opening.
  - It prints `{ "rows": [...], "error": null | "why" }`.
  - Live rows are `{ kind: "space", space, name }` or `{ kind: "terminal", address, space, tab, title, agent, status, snippet, cwd }`.
  - Sessions rows are `{ kind: "session", id, agent, session_id, title, space_name, updated, snippet, folder, argv, address }`. `argv` is the resume command, and `address` is the terminal running it, or null.
- **Addresses** from another machine are used as `<machine>/<id>` for terminals, tabs and spaces. A session's space is found among `spaces()` with the same `machine` whose `folder` is the session's, or whose tab has a pane (`panes()`) there.
- **Herdr functions:** `exec`, `machines`, `spaces`, `panes`, `agents`, `run`, `notify`, `theme`, `props` and `closeOverlay`.
- **Tab:** the app binds Tab to moving focus, and its binding runs before a mod's key handler. So `init` calls `cx.bind_keys([{ keystroke: "tab", action: "omnisearch_mode", context: "OmniSearch" }])`, and the root has `.key_context("OmniSearch").on_action("omnisearch_mode", …)`. Being deeper in, that binding wins. The `win98` menu mod does the same for Escape.
- **UI:**
  - `InputState` and `Input` from `gpui-base` for the field.
  - A focus-tracked root `v_flex` that takes Escape, arrows and page keys before the field does, and Tab too when the key binding is gone.
  - The shipped `mods/workspace-chrome/search.svg` and `spinner.svg`.
  - When the list is longer than 8 rows, every row is placed absolutely at `index × 46px` minus the scroll.
- **`herdr-omnisearch`** comes from https://github.com/dmnkf/herdr-omnisearch, installed with `uv tool install git+https://github.com/dmnkf/herdr-omnisearch`:
  - It reads this machine's herdr-core terminals with `hc ls`, `hc get` and `hc read --lines 0 --json`, and leaves out a connected machine's (`vps/t7`): that machine indexes its own.
  - It keeps them in `app-index.sqlite3`, apart from the Herdr plugin's `index.sqlite3`, so any plugin version on the same machine leaves them alone. The session catalog is shared.
  - It finds each Claude Code terminal's session through `~/.claude/sessions/<pid>.json`.
  - It searches screens and transcripts with SQLite full-text search and typo-tolerant matching.

## Watch out

- The mod runs a program on every connected machine, so tell the person before adding it. A machine without `herdr-omnisearch` on the core's `PATH` is skipped until the popup opens again. `uv tool install` puts the command in `~/.local/bin`.
- Session search needs the archive turned on in OmniSearch's `config.ini` (`[archive] enabled = true`). Otherwise the Sessions list shows that message.
- Only terminals Herdr shows are listed: the ones with `herdr.space` labels, and sub-agents under them. A plain shell with no agent is left out, as in OmniSearch's Herdr TUI plugin.
- Claude Code doesn't report OSC 7501 status in every version. Its status then comes from its session file, mapped as busy to working, waiting to blocked and idle to idle.
- Only each terminal's screen is read, never its history. herdr-core reads a full-screen app's history by scrolling it, which would scroll the person's agent under their hands. What a terminal said earlier comes from its session transcript instead, for Claude Code and for sessions started with a resume command.
- `exec` gives up after 30 s, and herdr-core runs at most 16 at once per machine. A search takes about 40 ms and a refresh about 55 ms, plus the trip to the machine.
- A background catalog refresh started through `hc exec` must drop `HC_SCOPE`, or herdr-core ends it with the command that started it.
- Ctrl+Shift+F is taken away from terminal programs. Pick another key in `keys.json` if one needs it.

## Done when

- `herdr-app check mods/omnisearch` passes with the matching Herdr binary and home.
- Ctrl+Shift+F opens the popup with the field focused, and an agent terminal on a connected machine with `herdr-omnisearch` shows up within a second.
- Typing a word from an agent's screen, or from something said earlier in its session, lists that terminal first. Enter focuses its pane in its tab.
- Tab lists old sessions. Enter on one starts its resume command in a new tab of the space on its folder, or in a new space on its machine.
- With `herdr-omnisearch` on no machine, the popup shows the install command instead of an empty list.
- Esc closes the popup and typing in terminals works as before.
