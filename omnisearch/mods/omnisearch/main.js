// OmniSearch: find a terminal by what it shows or said, or an old agent session
// to resume. Asks `herdr-omnisearch app-search` on this computer and on every
// connected machine; a machine without it is skipped. Live rows are the app's
// terminals and the herdr TUI's agent panes, each saying which it lives in.
// Tab switches between live terminals and old sessions, ↑/↓ and Enter go, Esc leaves.

import { View, div, image, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input, TextView } from "gpui-base";
import { exec, knownAgents, machines, panes, agents, spaces, run, notify, theme, props, closeOverlay } from "herdr";

const CLI = "herdr-omnisearch";
const INSTALL = "uv tool install git+https://github.com/dmnkf/herdr-omnisearch";
const LIMIT = 40;
const DEBOUNCE_MS = 90;
const LAUNCHERS_WAIT_MS = 5000;
const ROW_H = 36;
const ROW_SNIPPET_H = 54;
const HEADER_H = 30;
const CHROME_H = 92;
const SCREEN_LINE_H = 16;
const SCREEN_REFRESH_MS = 2000;
const CONVERSATION_REFRESH_MS = 3000;
const MODES = [["live", "Live"], ["sessions", "Sessions"]];
const MODE_ACTION = "omnisearch_mode";
const URGENCY = { blocked: 0, working: 1, done: 2, idle: 3 };
// Terminal art that reads as noise out of context: box drawing, blocks, braille, private-use glyphs, bullets.
const NOISE = /[─-▟⠀-⣿-⎿⏺○-◓●•›❯✳-✽✢-✧⚙]/g;

// Remote ids carry their machine: `vps/t7`.
const qualify = (machine, id) => (machine && id ? `${machine}/${id}` : id);

function ago(seconds) {
  const s = Math.max(0, Date.now() / 1000 - seconds);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  if (s < 30 * 86400) return `${Math.round(s / 86400)}d ago`;
  const d = new Date(seconds * 1000);
  const month = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(" ")[d.getMonth()];
  return d.getFullYear() === new Date().getFullYear() ? `${month} ${d.getDate()}` : `${month} ${d.getFullYear()}`;
}

function cleanSnippet(text) {
  return (text || "")
    .replace(NOISE, " ")
    .replace(/\*\*|__|`/g, "")
    .replace(/\bassistant:\s*/g, "")
    .replace(/\buser:\s*/g, "You: ")
    .replace(/^\s*(\.\.\.|…)\s*/, "…")
    .replace(/\s*(\.\.\.|…)\s*$/, "…")
    .replace(/\s+/g, " ")
    .trim();
}

const hits = (text, words) => words.length > 0 && words.some((w) => text.toLowerCase().includes(w.toLowerCase()));

// Starts the text shortly before its first match, so a one-line snippet always shows it.
function aroundFirstHit(text, words, before = 40) {
  const lower = text.toLowerCase();
  const at = Math.min(...words.map((w) => lower.indexOf(w.toLowerCase())).filter((i) => i >= 0));
  if (!Number.isFinite(at) || at <= before) return text;
  const start = text.lastIndexOf(" ", at - before);
  return "…" + text.slice(start < 0 ? at - before : start + 1);
}

// The excerpt as words, each marked when it holds a query word; the first match sits near the start.
function excerptWords(text, words, max = 90) {
  return aroundFirstHit(text, words, 120).split(" ").filter(Boolean).slice(0, max).map((w) => [w, hits(w, words)]);
}

const SGR = /\x1b\[([0-9;:]*)m/g;
const plain = (line) => line.replace(SGR, "");

// The screen as lines worth reading: blank runs folded to one, the part around the last match if any, else the end.
function screenLines(text, words, count) {
  const lines = [];
  for (const raw of (text || "").split(/\r?\n/)) {
    const line = raw.replace(/\s+$/, "");
    if (!plain(line).trim() && (!lines.length || !plain(lines[lines.length - 1]).trim())) continue;
    lines.push(line);
  }
  while (lines.length && !plain(lines[lines.length - 1]).trim()) lines.pop();
  let end = lines.length;
  for (let i = lines.length - 1; i >= 0 && words.length; i--) {
    if (hits(plain(lines[i]), words)) {
      end = Math.min(lines.length, i + Math.floor(count / 2));
      break;
    }
  }
  return lines.slice(Math.max(0, end - count), end);
}

// xterm's 256 colors: the theme's 16, a 6×6×6 cube, then a gray ramp.
function color256(t, n) {
  if (n < 16) return t[`ansi${n}`];
  if (n >= 232) {
    const v = (8 + (n - 232) * 10).toString(16).padStart(2, "0");
    return `#${v}${v}${v}`;
  }
  const c = n - 16;
  const level = (i) => (i === 0 ? 0 : 55 + i * 40).toString(16).padStart(2, "0");
  return `#${level(Math.floor(c / 36))}${level(Math.floor(c / 6) % 6)}${level(c % 6)}`;
}

const hex = (r, g, b) => `#${[r, g, b].map((v) => Number(v).toString(16).padStart(2, "0")).join("")}`;

// One line of `herdr pane read --ansi` as styled runs: colors, bold and dim.
function ansiRuns(t, line) {
  const out = [];
  let style = {};
  let last = 0;
  const push = (text) => text && out.push([text, style]);
  for (const m of line.matchAll(SGR)) {
    push(line.slice(last, m.index));
    last = m.index + m[0].length;
    const codes = (m[1] || "0").split(/[;:]/).map(Number);
    style = { ...style };
    for (let i = 0; i < codes.length; i++) {
      const c = codes[i];
      if (c === 0) style = {};
      else if (c === 1) style.bold = true;
      else if (c === 2) style.dim = true;
      else if (c === 22) style.bold = style.dim = false;
      else if (c >= 30 && c <= 37) style.fg = t[`ansi${c - 30}`];
      else if (c >= 90 && c <= 97) style.fg = t[`ansi${c - 82}`];
      else if (c === 39) delete style.fg;
      else if (c >= 40 && c <= 47) style.bg = t[`ansi${c - 40}`];
      else if (c >= 100 && c <= 107) style.bg = t[`ansi${c - 92}`];
      else if (c === 49) delete style.bg;
      else if ((c === 38 || c === 48) && codes[i + 1] === 5) {
        style[c === 38 ? "fg" : "bg"] = color256(t, codes[i + 2]);
        i += 2;
      } else if ((c === 38 || c === 48) && codes[i + 1] === 2) {
        style[c === 38 ? "fg" : "bg"] = hex(codes[i + 2], codes[i + 3], codes[i + 4]);
        i += 4;
      }
    }
  }
  push(line.slice(last));
  return out;
}

// The snippet split into plain and matched runs, for the query's words.
function runs(text, words) {
  if (!words.length) return [[text, false]];
  const pattern = new RegExp(`(${words.map((w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
  return text.split(pattern).filter(Boolean).map((part) => [part, words.some((w) => w.toLowerCase() === part.toLowerCase())]);
}

// A stable color per space name, from the theme's terminal palette, so one workspace's rows group at a glance.
function spaceColor(t, name) {
  // No yellow (it is the match color) and no red (it reads as an error).
  const palette = [t.ansi4, t.ansi5, t.ansi6, t.ansi2, t.ansi12, t.ansi13, t.ansi14, t.ansi10].filter(Boolean);
  if (!name || !palette.length) return t.dim;
  let h = 0;
  for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return palette[h % palette.length];
}

// The agent's icon from the app's program table: a one-color glyph or a picture.
function icon(p, size, color) {
  if (!p?.icon) return div().size(size).flex_none();
  return p.tint ? svg(p.icon).size(size).flex_none().text_color(color) : image(p.icon).w(size).h(size).flex_none();
}

// Agents the app starts through a launcher of their own are known by that program's name.
const launcherArgs = (launchers) => Object.entries(launchers).flatMap(([agent, run]) => ["--launcher", `${agent}=${JSON.stringify(run)}`]);

function answer(r) {
  if (r.code !== 0) return { rows: [], error: (r.stderr || r.stdout).trim().split("\n").pop() || `exit ${r.code}` };
  try {
    return JSON.parse(r.stdout);
  } catch {
    return { rows: [], error: "herdr-omnisearch printed no JSON" };
  }
}

// A space gets its own row only when it matched without any of its terminals.
function items(machine, rows) {
  const names = {};
  const withTerminals = new Set();
  for (const r of rows) {
    if (r.kind === "space") names[r.space] = r.name;
    if (r.kind === "terminal") withTerminals.add(r.space);
  }
  return rows
    .filter((r) => r.kind !== "space" || !withTerminals.has(r.space))
    .map((r) => ({
      ...r,
      machine,
      key: `${machine ?? ""}|${r.kind}|${r.address ?? r.id ?? r.space}`,
      spaceName: r.space_name ?? names[r.space] ?? r.name ?? "",
      clean: cleanSnippet(r.snippet),
    }));
}

export default class OmniSearch extends View {
  init(_props, cx) {
    this.mode = "live";
    this.query = "";
    this.selected = null;
    this.scroll = 0;
    this.typed = 0;
    this.ticket = 0;
    // Each machine's newest search still out: an older answer arriving late is dropped.
    this.latest = new Map();
    this.missing = new Set();
    this.found = new Map();
    this.errors = new Map();
    this.refreshErrors = new Map();
    this.launchers = new Map();
    this.programs = new Map();
    // What each live row's terminal shows now, read when it is selected.
    this.screens = new Map();
    // Each selected session's messages, as the catalog has them, for the words searched.
    this.conversations = new Map();
    this.size = { width: 1400, height: 800 };
    this.screenRows = 30;
    this.previewing = null;
    cx.timer.every(250, (cx) => this.watchPreview(cx));
    this.input = InputState.new({ placeholder: "Search agents by what they show or said" });
    this.input.on("change", (_e, cx) => {
      this.query = this.input.value();
      this.selected = null;
      this.scroll = 0;
      this.schedule(cx);
    });
    this.input.on("submit", (_e, cx) => this.go(this.current(), cx));
    this.input.focus();
    cx.timer.after(600, () => this.input.focus());
    this.handle = cx.focus_handle();
    // The app's Tab moves focus before our key handler sees it: a binding of our own, deeper in, comes first.
    try {
      cx.bind_keys([{ keystroke: "tab", action: MODE_ACTION, context: "OmniSearch" }]);
    } catch {}
    cx.spawn(async (cx) => {
      try {
        for (const p of await knownAgents()) this.programs.set(p.key, p);
      } catch {}
      cx.notify();
    });
    this.searchAll(cx);
    this.refresh(cx);
    for (const machine of this.targets()) this.launchersOn(machine, cx);
  }

  // How the person starts each agent on a machine (programs.json `run`), asked once; none if it takes too long.
  launchersOn(machine, cx) {
    if (!this.launchers.has(machine)) {
      let settle;
      this.launchers.set(machine, new Promise((resolve) => (settle = resolve)));
      cx.timer.after(LAUNCHERS_WAIT_MS, () => settle({}));
      cx.spawn(async () => {
        let known = [];
        try {
          known = await knownAgents({ on: machine });
        } catch {}
        const launchers = Object.fromEntries(known.filter((a) => a.installed).map((a) => [a.key, a.run]));
        settle(launchers);
        // Also an answer that came after the wait gave up, for the next resume.
        this.launchers.set(machine, Promise.resolve(launchers));
      });
    }
    return this.launchers.get(machine);
  }

  // The agent's own launcher, unless OmniSearch's resume command names another program.
  resumeArgv(item, launchers) {
    const launcher = launchers[item.agent];
    return launcher && item.argv[0] === item.agent ? [...launcher, ...item.argv.slice(1)] : item.argv;
  }

  close() {
    closeOverlay(props().overlay);
  }

  targets() {
    const remote = machines().filter((m) => m.name && m.layout !== "unavailable").map((m) => m.name).sort();
    return [null, ...remote].filter((m) => !this.missing.has(m));
  }

  // Shows what each machine already indexed at once, then searches again once it has re-read its terminals.
  refresh(cx) {
    for (const machine of this.targets()) {
      cx.spawn(async (cx) => {
        const launchers = launcherArgs(await this.launchersOn(machine, cx));
        try {
          this.refreshErrors.set(machine, answer(await exec(CLI, ["app-search", "--refresh", "--limit", "0", ...launchers], { on: machine })).error);
        } catch (e) {
          // A timeout or a lost connection; a missing command shows when its search fails too.
          this.refreshErrors.set(machine, `refresh failed: ${e?.message ?? e}`);
        }
        this.search(cx, machine);
      });
    }
  }

  schedule(cx) {
    const typed = ++this.typed;
    cx.timer.after(DEBOUNCE_MS, (cx) => {
      if (typed === this.typed) this.searchAll(cx);
    });
  }

  searchAll(cx) {
    for (const machine of this.targets()) this.search(cx, machine);
  }

  search(cx, machine) {
    const ticket = ++this.ticket;
    this.latest.set(machine, ticket);
    const args = ["app-search", "--limit", String(LIMIT)];
    if (this.mode === "sessions") args.push("--archive");
    args.push("--", ...this.words());
    cx.spawn(async (cx) => {
      let found = { rows: [], error: null };
      let reached = true;
      try {
        found = answer(await exec(CLI, args, { on: machine }));
      } catch {
        reached = false;
      }
      if (this.latest.get(machine) !== ticket) return;
      this.latest.delete(machine);
      // Not installed there, or out of reach: left out until the next open.
      if (!reached) this.missing.add(machine);
      this.errors.set(machine, found.error);
      this.found.set(machine, items(machine, found.rows));
      cx.notify();
    });
    cx.notify();
  }

  // Reads what the selected row shows once it is chosen, and again every few seconds while it stays chosen:
  // its conversation when the session is known, else the terminal's screen.
  watchPreview(cx) {
    const item = this.current();
    if (!item || this.previewing) return;
    const conversation = item.session_id ? this.conversations.get(item.key) : null;
    const query = this.words().join(" ");
    if (item.session_id && (!conversation || conversation.query !== query ||
        (item.kind === "terminal" && Date.now() - conversation.at > CONVERSATION_REFRESH_MS))) {
      return this.load(cx, item, exec(CLI, ["app-conversation", "--agent", item.agent, "--session", item.session_id, "--", ...this.words()], { on: item.machine, timeout: 5000 }),
        (r) => {
          let found = { found: false, messages: [] };
          try {
            if (r?.code === 0) found = JSON.parse(r.stdout);
          } catch {}
          this.conversations.set(item.key, { ...found, query, at: Date.now() });
        });
    }
    if (item.kind !== "terminal" || conversation?.found) return;
    const seen = this.screens.get(item.key);
    if (seen && Date.now() - seen.at < SCREEN_REFRESH_MS) return;
    const read = item.frontend === "tui"
      ? exec("herdr", ["pane", "read", item.address, "--ansi", "--source", "visible"], { on: item.machine, timeout: 5000 })
      : exec("hc", ["read", qualify(item.machine, item.address), "--lines", "0"], { timeout: 5000 });
    this.load(cx, item, read, (r) => this.screens.set(item.key, { text: r?.code === 0 ? r.stdout : null, at: Date.now() }));
  }

  load(cx, item, pending, keep) {
    this.previewing = item.key;
    cx.spawn(async (cx) => {
      let r = null;
      try {
        r = await pending;
      } catch {}
      keep(r);
      this.previewing = null;
      cx.notify();
    });
  }

  words() {
    return this.query.trim().split(/\s+/).filter(Boolean);
  }

  // One section per machine, in a fixed order, so a machine answering later never moves the others.
  sections() {
    const browsing = this.mode === "live" && !this.words().length;
    return this.targets()
      .map((machine) => {
        let rows = this.found.get(machine) ?? [];
        if (browsing) rows = [...rows].sort((a, b) => (URGENCY[a.status] ?? 4) - (URGENCY[b.status] ?? 4));
        return { machine, rows };
      })
      .filter((s) => s.rows.length);
  }

  list() {
    return this.sections().flatMap((s) => s.rows);
  }

  current() {
    const list = this.list();
    return list.find((i) => i.key === this.selected) ?? list[0];
  }

  move(by) {
    const list = this.list();
    if (!list.length) return;
    const at = Math.max(0, list.findIndex((i) => i.key === this.current()?.key));
    this.selected = list[Math.max(0, Math.min(at + by, list.length - 1))].key;
  }

  setMode(mode, cx) {
    if (mode === this.mode) return;
    this.mode = mode;
    this.selected = null;
    this.scroll = 0;
    this.found = new Map();
    this.errors = new Map();
    this.searchAll(cx);
  }

  go(item, cx) {
    if (!item) return;
    if (item.kind === "session") {
      // An older OmniSearch names only the app terminal running it.
      const running = item.running ?? (item.address ? { frontend: "core", address: item.address } : null);
      return running ? this.goLive(item.machine, running, cx) : this.resume(item, cx);
    }
    if (item.kind === "space") {
      this.close();
      return run("space.select", { id: qualify(item.machine, item.space) });
    }
    this.goLive(item.machine, item, cx);
  }

  // A terminal in the app, or a pane of the herdr TUI: through the app terminal mirroring it, else in the TUI itself.
  goLive(machine, where, cx) {
    if (where.frontend !== "tui" || where.mirror) {
      this.close();
      return where.frontend === "tui" ? this.focusTerminal(machine, where.mirror, null) : this.focusTerminal(machine, where.address, where.tab);
    }
    const args = ["focus-target", "--workspace-id", where.space ?? "", "--tab-id", where.tab ?? "", "--pane-id", where.address];
    if (where.agent) args.push("--agent", where.agent);
    if (where.socket) args.push("--socket", where.socket);
    cx.spawn(async () => {
      let r;
      try {
        r = await exec(CLI, args, { on: machine });
      } catch (e) {
        r = { code: 1, stderr: `${e?.message ?? e}` };
      }
      this.close();
      const name = where.title || where.address;
      const tui = machine ? `the herdr TUI on ${machine}` : "the herdr TUI";
      if (r.code === 0) notify({ title: "OmniSearch", body: `${name} runs in ${tui}, not in the app. It is selected there now: switch to that TUI to see it.` });
      else notify({ title: "OmniSearch", body: `Couldn't select ${name} in ${tui}: ${(r.stderr || r.stdout || "").trim() || `exit ${r.code}`}`, level: "warning" });
    });
  }

  focusTerminal(machine, address, tab) {
    const terminal = qualify(machine, address);
    if (panes().some((p) => p.terminal === terminal)) {
      if (tab) run("tab.select", { id: qualify(machine, tab) });
      run("pane.focus", { terminal });
    } else if (agents().some((a) => a.address === terminal)) run("agent.focus", { terminal });
    else run("terminal.peek", { terminal });
  }

  resume(item, cx) {
    if (!item.argv) {
      this.close();
      notify({ title: "OmniSearch", body: `No resume command for ${item.agent}`, level: "warning" });
      return;
    }
    if (this.resuming) return;
    this.resuming = true;
    cx.notify();
    // Closing the popup ends its tasks, so it waits for the machine's launchers first.
    cx.spawn(async (cx) => {
      const launchers = await this.launchersOn(item.machine, cx);
      // The list can be a refresh behind the catalog: ask again, so a running session is focused, never started twice.
      let found;
      try {
        const check = ["app-search", "--refresh", "--limit", "0", "--running", `${item.agent}=${item.session_id}`, ...launcherArgs(launchers)];
        found = answer(await exec(CLI, check, { on: item.machine }));
      } catch (e) {
        found = { rows: [], error: `${e?.message ?? e}` };
      }
      const running = found.rows.find((r) => r.address);
      if (running) return this.goLive(item.machine, running, cx);
      this.close();
      // Only a check that answered may start it: one that failed can't tell a running session from an ended one.
      if (found.error) return notify({ title: "OmniSearch", body: `Didn't resume ${item.title || item.session_id}: ${found.error}`, level: "warning" });
      const tabsThere = (s) => new Set(s.tabs.map((t) => t.id));
      const space = spaces().find(
        (s) => (s.machine ?? null) === item.machine && (s.folder === item.folder || panes().some((p) => p.cwd === item.folder && tabsThere(s).has(p.tab))),
      );
      const argv = this.resumeArgv(item, launchers);
      if (space) run("tab.new", { space: space.id, folder: item.folder, argv });
      else run("space.new", { folder: item.folder, argv, ...(item.machine ? { machine: item.machine } : {}) });
    });
  }

  program(agent) {
    return this.programs.get(agent) ?? (agent ? { icon: `icons/${agent}.svg`, tint: false } : { icon: "icons/agent.svg", tint: true });
  }

  // Only what asks for attention is drawn; idle says nothing.
  status(t, item) {
    if (item.kind === "session") {
      const running = item.running ?? item.address;
      return running ? div().size(6).mx(3).flex_none().rounded(3).bg(t.working) : null;
    }
    if (item.status === "working") return svg("mods/workspace-chrome/spinner.svg").size(12).flex_none().text_color(t.working).spin(900);
    if (item.status === "blocked") return div().flex_none().px(6).h(18).rounded(5).bg(t.blocked + "26").text_size(10.5).font_semibold().text_color(t.blocked).flex().items_center().child("Needs you");
    if (item.status === "done") return div().size(6).mx(3).flex_none().rounded(3).bg(t.done);
    return null;
  }

  rowHeight(item) {
    return this.showSnippet(item) ? ROW_SNIPPET_H : ROW_H;
  }

  showSnippet(item) {
    return item.kind !== "space" && Boolean(item.clean) && (this.mode === "sessions" || this.words().length > 0);
  }

  row(t, item, selected) {
    let title;
    let space = "";
    let suffix = "";
    if (item.kind === "space") {
      title = item.spaceName;
      suffix = "Space";
    } else if (item.kind === "terminal") {
      title = item.title || item.address;
      space = item.spaceName;
      suffix = item.frontend === "tui" && !item.mirror ? "TUI" : "";
    } else {
      title = item.title || item.spaceName || item.session_id;
      const running = item.running ?? (item.address ? { frontend: "core" } : null);
      const inTui = running?.frontend === "tui" && !running.mirror;
      space = item.title ? item.spaceName : "";
      suffix = running ? (inTui ? "running in TUI" : "running") : ago(item.updated);
    }
    const snippet = this.showSnippet(item);
    const status = this.status(t, item);
    return v_flex()
      .mx(6)
      .px(10)
      .justify_center()
      .gap(3)
      .rounded(8)
      .cursor_pointer()
      .relative()
      .when(selected, (el) => el.bg(t.selected).child(div().absolute().left(0).top(8).bottom(8).w(3).rounded(2).bg(t.accent)))
      .when(!selected, (el) => el.hover((s) => s.bg(t.hover)))
      .on_click((_e, cx) => this.go(item, cx))
      .child(
        h_flex()
          .gap(10)
          .items_center()
          .child(item.kind === "space" ? svg("mods/workspace-chrome/layers.svg").size(16).flex_none().text_color(t.muted) : icon(this.program(item.agent), 16, t.foreground))
          .child(div().flex_1().min_w(0).truncate().text_size(13.5).text_color(t.foreground).child(title))
          .when(Boolean(status), (el) => el.child(status))
          .child(
            h_flex()
              .flex_none()
              .gap(6)
              .text_size(12)
              .when(Boolean(space), (el) => el.child(div().max_w(180).truncate().text_color(spaceColor(t, space)).child(space)))
              .when(Boolean(suffix), (el) => el.child(div().text_color(t.dim).child(suffix))),
          ),
      )
      .when(snippet, (el) =>
        el.child(
          h_flex()
            .pl(26)
            .overflow_hidden()
            .whitespace_nowrap()
            .text_size(12)
            .children(runs(aroundFirstHit(item.clean, this.words()).slice(0, 220), this.words()).map(([text, hit]) =>
              div().flex_none().text_color(hit ? t.accent : t.dim).when(hit, (d) => d.font_medium()).child(text))),
        ),
      );
  }

  preview(t, item) {
    if (!item) return v_flex().flex_1();
    const words = this.words();
    const label = (text) => div().text_size(11).font_medium().text_color(t.dim).child(text);
    let title;
    const meta = [];
    if (item.kind === "space") {
      title = item.spaceName;
      meta.push("Space");
    } else if (item.kind === "terminal") {
      title = item.title || item.address;
      meta.push(item.spaceName, item.frontend === "tui" ? (item.mirror ? "TUI, shown here" : "in the TUI") : "in the app", item.status);
    } else {
      title = item.title || item.spaceName || item.session_id;
      const running = item.running ?? item.address;
      meta.push(item.spaceName, running ? "running" : ago(item.updated));
    }
    if (item.machine) meta.push(item.machine);
    const conversation = item.session_id ? this.conversations.get(item.key) : null;
    const talk = conversation?.found && conversation.messages.length ? conversation.messages : null;
    const screen = item.kind === "terminal" && !talk ? this.screens.get(item.key) : null;
    const lines = screen?.text ? screenLines(screen.text, words, this.screenRows) : [];
    const excerpt = item.clean && (words.length || item.kind === "session") ? item.clean : "";
    return v_flex()
      .flex_1()
      .min_w(0)
      .h(this.listH())
      .overflow_hidden()
      .px(20)
      .py(16)
      .gap(14)
      .child(
        v_flex()
          .gap(5)
          .child(
            h_flex()
              .gap(9)
              .items_center()
              .child(item.kind === "space" ? svg("mods/workspace-chrome/layers.svg").size(18).flex_none().text_color(t.muted) : icon(this.program(item.agent), 18, t.foreground))
              .child(div().flex_1().min_w(0).text_size(14).font_medium().text_color(t.foreground).truncate().child(title)),
          )
          .child(
            h_flex()
              .pl(27)
              .gap(6)
              .text_size(12)
              .when(item.kind !== "space" && Boolean(item.spaceName), (el) => el.child(div().flex_none().text_color(spaceColor(t, item.spaceName)).child(item.spaceName)))
              .child(div().min_w(0).truncate().text_color(t.dim).child(meta.filter((m, i) => m && !(i === 0 && m === item.spaceName)).join(" · "))),
          ),
      )
      .when(Boolean(excerpt), (el) =>
        el.child(
          v_flex()
            .gap(6)
            .child(label(item.kind === "session" ? "Conversation" : "Match"))
            .child(
              h_flex()
                .flex_wrap()
                .max_h(154)
                .overflow_hidden()
                .gap(4)
                .text_size(12.5)
                .children(excerptWords(excerpt, words).map(([word, hit], i) =>
                  div()
                    .id(`os-w-${i}`)
                    .h(18)
                    .line_height("18px")
                    .text_color(hit ? t.foreground : t.muted)
                    .when(hit, (d) => d.px(3).rounded(4).bg(t.accent + "40"))
                    .child(word))),
            ),
        ),
      )
      .when(Boolean(talk), (el) => el.child(this.conversationView(t, item, talk || [])))
      .when(item.kind === "terminal" && !talk, (el) =>
        el.child(
          v_flex()
            .flex_1()
            .min_h(0)
            .gap(6)
            .child(label("Screen"))
            .child(
              v_flex()
                .id("os-screen")
                .flex_1()
                .min_h(0)
                .overflow_hidden()
                .rounded(8)
                .bg(t.background)
                .px(6)
                .py(8)
                .on_size_change((size, cx) => {
                  const rows = Math.max(4, Math.floor((size.height - 16) / SCREEN_LINE_H));
                  if (rows !== this.screenRows) {
                    this.screenRows = rows;
                    cx.notify();
                  }
                })
                .font_family("JetBrains Mono")
                .text_size(11.5)
                .children(
                  lines.length
                    ? lines.map((line, i) => {
                        const hit = hits(plain(line), words);
                        return h_flex().id(`os-l-${i}`).h(SCREEN_LINE_H).line_height(`${SCREEN_LINE_H}px`).flex_none().px(4).rounded(3).whitespace_nowrap().overflow_hidden()
                          .text_color(t.foreground).when(hit, (d) => d.bg(t.accent + "30"))
                          .children(ansiRuns(t, line).map(([text, st], j) =>
                            div().id(`os-r-${i}-${j}`).flex_none()
                              .when(Boolean(st.fg), (d) => d.text_color(st.fg))
                              .when(Boolean(st.bg), (d) => d.bg(st.bg))
                              .when(Boolean(st.bold), (d) => d.font_bold())
                              .when(Boolean(st.dim), (d) => d.opacity(0.6))
                              .child(text)));
                      })
                    : [div().text_color(t.dim).child(screen ? "Nothing on screen" : "Reading…")],
                ),
            ),
        ),
      );
  }

  listH() {
    return Math.max(200, this.size.height - CHROME_H);
  }

  // The session's messages, newest at the bottom: yours on a tinted block, the agent's as rendered Markdown.
  conversationView(t, item, messages) {
    const agentName = this.program(item.agent)?.name || item.agent || "Agent";
    return v_flex()
      .flex_1()
      .min_h(0)
      .gap(6)
      .child(div().text_size(11).font_medium().text_color(t.dim).child("Conversation"))
      .child(
        v_flex()
          .id("os-conv")
          .flex_1()
          .min_h(0)
          .overflow_hidden()
          .justify_end()
          .gap(12)
          .children(messages.map((m, i) => {
            const you = m.role === "user";
            return v_flex()
              .id(`os-m-${i}`)
              .flex_none()
              .gap(4)
              .px(12)
              .py(you ? 9 : 4)
              .rounded(8)
              .when(you, (d) => d.bg(t.selected))
              .when(Boolean(m.match), (d) => d.border_1().border_color(t.accent + "99"))
              .child(
                h_flex()
                  .gap(6)
                  .text_size(11)
                  .child(div().font_medium().text_color(you ? t.foreground : spaceColor(t, item.agent)).child(you ? "You" : agentName))
                  .when(Boolean(m.at), (d) => d.child(div().text_color(t.dim).child(ago(m.at)))),
              )
              .child(div().text_size(12.5).text_color(t.foreground).child(TextView.markdown(`os-md-${item.key}-${i}`, m.text)));
          })),
      );
  }

  problems() {
    return this.targets()
      .map((m) => [m, this.refreshErrors.get(m) || this.errors.get(m)])
      .filter(([, e]) => e)
      .map(([m, e]) => (m ? `${m}: ${e}` : e))
      .join(" · ");
  }

  notice() {
    if (!this.targets().length) return `OmniSearch isn't installed here or on a connected machine. Install it where your agents run: ${INSTALL}`;
    if (this.latest.size) return "Searching…";
    const q = this.query.trim();
    return this.problems() || (q ? `Nothing matches “${q}”` : this.mode === "live" ? "No agents running" : "No sessions yet");
  }

  toggleMode(cx) {
    this.setMode(this.mode === "live" ? "sessions" : "live", cx);
  }

  render() {
    const t = theme();
    const sections = this.sections();
    const list = sections.flatMap((s) => s.rows);
    const current = this.current();
    const headed = sections.length > 1 || (sections[0] && sections[0].machine);

    // Every row and header gets a fixed top: GPUI rounds each height and a stack drifts.
    const layout = [];
    let y = 4;
    for (const s of sections) {
      if (headed) {
        layout.push({ header: s, top: y, h: HEADER_H });
        y += HEADER_H;
      }
      for (const item of s.rows) {
        const h = this.rowHeight(item);
        layout.push({ item, top: y, h });
        y += h;
      }
    }
    const listH = this.listH();
    const total = y + 4;
    this.maxScroll = Math.max(0, total - listH);
    const at = layout.find((l) => l.item && l.item.key === current?.key);
    if (at) {
      if (at.top - 4 < this.scroll) this.scroll = at.top - (layout[layout.indexOf(at) - 1]?.header ? HEADER_H + 4 : 4);
      if (at.top + at.h + 4 > this.scroll + listH) this.scroll = at.top + at.h + 4 - listH;
    }
    this.scroll = Math.max(0, Math.min(this.scroll, this.maxScroll));

    const placed = layout
      .filter((l) => l.top + l.h > this.scroll && l.top < this.scroll + listH)
      .map((l, i) => {
        const el = l.header
          ? h_flex()
              .id(`os-h-${l.header.machine ?? "here"}`)
              .px(16)
              .items_end()
              .pb(6)
              .gap(6)
              .text_size(11.5)
              .child(div().text_color(t.foreground).font_semibold().child(l.header.machine ?? "This computer"))
              .child(div().text_color(t.dim).child(String(l.header.rows.length)))
          : this.row(t, l.item, l.item.key === current?.key).id(`os-${l.item.key}`);
        return el.absolute().left(0).right(0).top(l.top - this.scroll).h(l.h);
      });

    const segment = ([mode, label]) =>
      div()
        .id(`os-mode-${mode}`)
        .px(9)
        .h(22)
        .flex()
        .items_center()
        .rounded(6)
        .text_size(12)
        .cursor_pointer()
        .text_color(mode === this.mode ? t.foreground : t.dim)
        .when(mode === this.mode, (el) => el.bg(t.selected))
        .when(mode !== this.mode, (el) => el.hover((s) => s.text_color(t.muted)))
        .on_click((_e, cx) => this.setMode(mode, cx))
        .child(label);

    const key = (cap, label) =>
      h_flex().gap(5).items_center()
        .child(div().px(5).h(17).flex().items_center().rounded(4).bg(t.hover).text_size(10.5).text_color(t.muted).child(cap))
        .child(div().text_color(t.dim).child(label));

    const busy = this.latest.size > 0 || Boolean(this.resuming);
    const counted = list.length ? `${list.length} ${this.mode === "live" ? (list.length === 1 ? "agent" : "agents") : list.length === 1 ? "session" : "sessions"}` : "";

    return v_flex()
      .id("omnisearch")
      .key_context("OmniSearch")
      .on_action(MODE_ACTION, (_e, cx) => this.toggleMode(cx))
      .track_focus(this.handle)
      .size_full()
      .on_size_change((size, cx) => {
        if (size.width !== this.size.width || size.height !== this.size.height) {
          this.size = size;
          cx.notify();
        }
      })
      .bg(t.popover)
      .text_size(13)
      .on_key_down((e, cx) => {
        const k = e.key;
        if (k === "escape") return this.close();
        if (k === "tab") this.toggleMode(cx);
        else if (k === "down") this.move(1);
        else if (k === "up") this.move(-1);
        else if (k === "pagedown") this.move(8);
        else if (k === "pageup") this.move(-8);
        else return;
        cx.stop_propagation();
        cx.notify();
      })
      .child(
        h_flex()
          .h(56)
          .flex_none()
          .px(18)
          .gap(12)
          .items_center()
          .child(svg("mods/workspace-chrome/search.svg").size(17).flex_none().text_color(t.dim))
          .child(Input.new(this.input).flex_1().h_full().text_size(16).text_color(t.foreground))
          .when(busy, (el) => el.child(svg("mods/workspace-chrome/spinner.svg").size(13).flex_none().text_color(t.dim).spin(900)))
          .child(h_flex().flex_none().p(2).gap(2).rounded(8).bg(t.background + "80").children(MODES.map(segment))),
      )
      .child(div().h(1).flex_none().bg(t.border))
      .child(h_flex().h(listH).flex_none().child(
        list.length
          ? div()
              .id("os-rows")
              .relative()
              .h(listH)
              .w(Math.max(420, Math.round(this.size.width * 0.4)))
              .flex_none()
              .overflow_hidden()
              .on_scroll_wheel((e, cx) => {
                const d = Math.abs(e.delta.y) < 4 ? e.delta.y * ROW_H : e.delta.y;
                this.scroll = Math.max(0, Math.min(this.scroll - d, this.maxScroll));
                cx.notify();
              })
              .children(placed)
          : v_flex().h(listH).w(Math.max(420, Math.round(this.size.width * 0.4))).flex_none().items_center().justify_center().px(40)
              .child(div().text_size(13).text_color(t.dim).text_center().child(this.notice())),
      ).child(div().w(1).h_full().flex_none().bg(t.border))
       .child(this.preview(t, current)))
      .child(div().h(1).flex_none().bg(t.border))
      .child(
        h_flex()
          .h(34)
          .flex_none()
          .px(16)
          .gap(14)
          .items_center()
          .text_size(11)
          .child(key("↵", "Open"))
          .child(key("⇥", this.mode === "live" ? "Sessions" : "Live"))
          .child(key("esc", "Close"))
          .child(div().flex_1())
          .child(div().max_w(380).truncate().text_color(this.problems() ? t.blocked : t.dim).child(this.problems() || counted)),
      );
  }
}
