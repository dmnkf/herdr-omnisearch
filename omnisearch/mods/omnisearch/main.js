// OmniSearch: find a terminal by what it shows or said, or an old agent session
// to resume. Asks `herdr-omnisearch app-search` on this computer and on every
// connected machine; a machine without it is skipped. Tab switches between
// live terminals and old sessions, ↑/↓ and Enter go, Esc leaves.

import { View, div, svg } from "gpui-kit";
import { v_flex, h_flex, InputState, Input } from "gpui-base";
import { exec, knownAgents, machines, panes, agents, spaces, run, notify, theme, props, closeOverlay } from "herdr";

const CLI = "herdr-omnisearch";
const INSTALL = "uv tool install git+https://github.com/dmnkf/herdr-omnisearch";
const LIMIT = 40;
const DEBOUNCE_MS = 90;
const LAUNCHERS_WAIT_MS = 5000;
const ROW_H = 46;
const LIST_H = ROW_H * 8;
const MODES = [["live", "Live"], ["sessions", "Sessions"]];
const MODE_ACTION = "omnisearch_mode";
const MARKS = { blocked: "●", done: "●", idle: "○" };

// Remote ids carry their machine: `vps/t7`.
const qualify = (machine, id) => (machine && id ? `${machine}/${id}` : id);

function ago(seconds) {
  const s = Math.max(0, Date.now() / 1000 - seconds);
  if (s < 3600) return `${Math.max(1, Math.round(s / 60))}m`;
  if (s < 86400) return `${Math.round(s / 3600)}h`;
  return `${Math.round(s / 86400)}d`;
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
    .map((r) => ({ ...r, machine, spaceName: r.space_name ?? names[r.space] ?? r.name ?? "" }));
}

export default class OmniSearch extends View {
  init(_props, cx) {
    this.mode = "live";
    this.query = "";
    this.at = 0;
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
    this.input = InputState.new({ placeholder: "Search terminals by what they show or said…" });
    this.input.on("change", (_e, cx) => {
      this.query = this.input.value();
      this.at = 0;
      this.schedule(cx);
    });
    this.input.on("submit", (_e, cx) => this.go(this.list()[this.at], cx));
    this.input.focus();
    cx.timer.after(600, () => this.input.focus());
    this.handle = cx.focus_handle();
    // The app's Tab moves focus before our key handler sees it: a binding of our own, deeper in, comes first.
    try {
      cx.bind_keys([{ keystroke: "tab", action: MODE_ACTION, context: "OmniSearch" }]);
    } catch {}
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
    const remote = machines().filter((m) => m.name && m.layout !== "unavailable").map((m) => m.name);
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
    args.push("--", ...this.query.trim().split(/\s+/).filter(Boolean));
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

  list() {
    return this.targets().flatMap((m) => this.found.get(m) ?? []);
  }

  setMode(mode, cx) {
    if (mode === this.mode) return;
    this.mode = mode;
    this.at = 0;
    this.scroll = 0;
    this.found = new Map();
    this.errors = new Map();
    this.searchAll(cx);
  }

  go(item, cx) {
    if (!item) return;
    if (item.kind === "session" && !item.address) return this.resume(item, cx);
    this.close();
    if (item.kind === "space") run("space.select", { id: qualify(item.machine, item.space) });
    else if (item.kind === "terminal") this.focusTerminal(item.machine, item.address, item.tab);
    else this.focusTerminal(item.machine, item.address, null);
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
      let running = null;
      try {
        const check = ["app-search", "--refresh", "--limit", "0", "--running", `${item.agent}=${item.session_id}`, ...launcherArgs(launchers)];
        running = answer(await exec(CLI, check, { on: item.machine })).rows.find((r) => r.address)?.address;
      } catch {}
      this.close();
      if (running) return this.focusTerminal(item.machine, running, null);
      const tabsThere = (s) => new Set(s.tabs.map((t) => t.id));
      const space = spaces().find(
        (s) => (s.machine ?? null) === item.machine && (s.folder === item.folder || panes().some((p) => p.cwd === item.folder && tabsThere(s).has(p.tab))),
      );
      const argv = this.resumeArgv(item, launchers);
      if (space) run("tab.new", { space: space.id, folder: item.folder, argv });
      else run("space.new", { folder: item.folder, argv, ...(item.machine ? { machine: item.machine } : {}) });
    });
  }

  glyph(t, status) {
    if (status === "working") return svg("mods/workspace-chrome/spinner.svg").size(12).flex_none().text_color(t.working).spin(900);
    return div().w(12).flex_none().text_color(MARKS[status] ? t[status] : t.dim).child(MARKS[status] ?? "·");
  }

  row(t, item, selected) {
    const tags = [];
    let title;
    if (item.kind === "space") title = item.spaceName;
    else if (item.kind === "terminal") {
      title = item.title || item.address;
      tags.push(item.agent, item.spaceName);
    } else {
      title = item.title || item.spaceName || item.session_id;
      tags.push(item.agent, item.title ? item.spaceName : "", item.address ? "running" : ago(item.updated));
    }
    if (item.machine) tags.push(item.machine);
    const snippet = item.kind === "space" ? "space" : item.snippet;
    return v_flex()
      .mx(4)
      .px(10)
      .justify_center()
      .gap(2)
      .rounded(7)
      .cursor_pointer()
      .when(selected, (el) => el.bg(t.selected))
      .hover((s) => s.bg(t.hover))
      .on_click((_e, cx) => this.go(item, cx))
      .child(
        h_flex()
          .gap(8)
          .items_center()
          .child(item.kind === "session" ? div().w(12).flex_none().text_color(item.address ? t.working : t.dim).child(item.address ? "●" : "↺") : this.glyph(t, item.status))
          .child(div().flex_1().min_w(0).truncate().text_color(t.foreground).when(selected, (d) => d.font_semibold()).child(title))
          .children(tags.filter(Boolean).map((tag) => div().flex_none().text_size(11).text_color(t.dim).child(tag))),
      )
      .child(div().pl(20).truncate().text_size(11.5).text_color(t.muted).child(snippet || " "));
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
    return this.problems() || (this.query.trim() ? `Nothing matches "${this.query.trim()}".` : "No terminals to show yet.");
  }

  toggleMode(cx) {
    this.setMode(this.mode === "live" ? "sessions" : "live", cx);
  }

  render() {
    const t = theme();
    const list = this.list();
    this.at = Math.min(this.at, Math.max(0, list.length - 1));
    const total = list.length * ROW_H;
    const fixed = total > LIST_H;
    this.maxScroll = Math.max(0, total - LIST_H);
    const top = this.at * ROW_H;
    if (top < this.scroll) this.scroll = top;
    if (top + ROW_H > this.scroll + LIST_H) this.scroll = top + ROW_H - LIST_H;
    this.scroll = Math.max(0, Math.min(this.scroll, this.maxScroll));
    // Rows sit at fixed edges, not stacked: GPUI rounds each height and a stack drifts.
    const place = (el, start) =>
      fixed ? el.absolute().left(0).right(0).top(start - this.scroll).h(ROW_H) : el.flex_none().h(ROW_H);
    const rows = [];
    list.forEach((item, i) => {
      const start = i * ROW_H;
      if (fixed && (start + ROW_H <= this.scroll || start >= this.scroll + LIST_H)) return;
      rows.push(place(this.row(t, item, i === this.at), start).id(`os-${i}`));
    });

    const chip = ([mode, label]) =>
      div()
        .id(`os-mode-${mode}`)
        .px(8)
        .py(2)
        .rounded(6)
        .text_size(12)
        .cursor_pointer()
        .text_color(mode === this.mode ? t.foreground : t.dim)
        .when(mode === this.mode, (el) => el.bg(t.selected))
        .hover((s) => s.text_color(t.foreground))
        .on_click((_e, cx) => this.setMode(mode, cx))
        .child(label);

    return v_flex()
      .id("omnisearch")
      .key_context("OmniSearch")
      .on_action(MODE_ACTION, (_e, cx) => this.toggleMode(cx))
      .track_focus(this.handle)
      .w_full()
      .min_h(0)
      .p(18)
      .gap(10)
      .bg(t.popover)
      .text_size(13)
      .on_key_down((e, cx) => {
        const k = e.key;
        if (k === "escape") return this.close();
        if (k === "tab") this.toggleMode(cx);
        else if (k === "down") this.at = Math.max(0, Math.min(this.at + 1, list.length - 1));
        else if (k === "up") this.at = Math.max(this.at - 1, 0);
        else if (k === "pagedown") this.at = Math.max(0, Math.min(this.at + 8, list.length - 1));
        else if (k === "pageup") this.at = Math.max(this.at - 8, 0);
        else return;
        cx.stop_propagation();
        cx.notify();
      })
      .child(
        h_flex()
          .items_center()
          .gap(6)
          .child(div().text_size(15).font_semibold().text_color(t.foreground).mr(6).child("OmniSearch"))
          .children(MODES.map(chip))
          .child(div().flex_1())
          .when(this.latest.size > 0 || Boolean(this.resuming), (el) => el.child(svg("mods/workspace-chrome/spinner.svg").size(12).text_color(t.dim).spin(900)))
          .child(div().text_size(11).text_color(t.dim).child("Tab live/sessions · Esc")),
      )
      .child(
        h_flex().h(36).flex_none().px(10).gap(8).items_center().rounded(9).border_1().border_color(t.accent).bg(t.background)
          .child(svg("mods/workspace-chrome/search.svg").size(15).text_color(t.dim))
          .child(Input.new(this.input).flex_1().h_full().text_size(12.8).text_color(t.foreground)),
      )
      .child(
        rows.length
          ? div()
              .id("os-rows")
              .relative()
              .mb(6)
              .when(fixed, (el) => el.h(LIST_H).overflow_hidden())
              .on_scroll_wheel((e, cx) => {
                const d = Math.abs(e.delta.y) < 4 ? e.delta.y * ROW_H : e.delta.y;
                this.scroll = Math.max(0, Math.min(this.scroll - d, this.maxScroll));
                while (this.at < list.length - 1 && this.at * ROW_H < this.scroll) this.at++;
                while (this.at > 0 && this.at * ROW_H + ROW_H > this.scroll + LIST_H) this.at--;
                cx.notify();
              })
              .children(fixed ? rows : [v_flex().children(rows)])
          : div().px(14).py(12).text_size(12.5).text_color(t.dim).child(this.notice()),
      )
      .when(rows.length > 0 && Boolean(this.problems()), (el) => el.child(div().px(14).truncate().text_size(11).text_color(t.dim).child(this.problems())));
  }
}
