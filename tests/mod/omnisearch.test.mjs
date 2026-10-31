import { test } from "node:test";
import assert from "node:assert/strict";

const { default: OmniSearch } = await import("../../omnisearch/mods/omnisearch/main.js");

function app({ machines = [{ name: null, layout: "ready" }], answers = {}, panes = [], agents = [], spaces = [], launchers = {} } = {}) {
  const state = {
    machines,
    panes,
    agents,
    spaces,
    ran: [],
    notified: [],
    closed: [],
    calls: [],
    knownAgents: async (options) => {
      const machine = options.on ?? "here";
      if (!(machine in launchers)) throw new Error("unreachable");
      return launchers[machine];
    },
    exec: async (command, args, options) => {
      state.calls.push({ command, args, on: options.on });
      const answer = answers[options.on ?? "here"];
      if (answer === undefined) throw new Error("not installed");
      return typeof answer === "function" ? answer(args) : answer;
    },
  };
  globalThis.__herdr = state;
  return state;
}

function cx() {
  const c = {
    timers: [],
    tasks: [],
    notified: 0,
    stopped: false,
    bound: [],
    bind_keys: (bindings) => (c.bound.push(...bindings), bindings.length),
    timer: { after: (ms, fn) => c.timers.push(Object.assign(fn, { ms })), every: () => {} },
    spawn: (fn) => c.tasks.push(fn(c)),
    notify: () => c.notified++,
    focus_handle: () => ({ focus: () => {} }),
    stop_propagation: () => (c.stopped = true),
  };
  return c;
}

async function settle(c) {
  while (c.tasks.length) await c.tasks.shift();
}

function ok(rows) {
  return { code: 0, stdout: JSON.stringify({ rows, error: null }), stderr: "" };
}

async function open(state) {
  const view = new OmniSearch();
  const c = cx();
  view.init({}, c);
  await settle(c);
  return { view, c };
}

async function type(view, c, text) {
  globalThis.__input.text = text;
  globalThis.__input.fire("change", c);
  c.timers.pop()(c);
  await settle(c);
}

async function enter(c) {
  globalThis.__input.fire("submit", c);
  await settle(c);
}

function key(view, c, k) {
  view.render().handler("on_key_down")({ key: k, modifiers: {} }, c);
}

const TERMINAL = { kind: "terminal", address: "t3", space: "sp1", tab: "tb1", title: "fix login", agent: "claude", status: "working", snippet: "the login form", cwd: "/repo" };

test("opening searches every reachable machine, refreshes each, and searches again once it has", async () => {
  const state = app({
    machines: [{ name: null, layout: "ready" }, { name: "vps", layout: "ready" }, { name: "off", layout: "unavailable" }],
    answers: { here: ok([]), vps: ok([]) },
  });
  await open(state);
  const search = ["app-search", "--limit", "40", "--"];
  assert.deepEqual(state.calls.map((call) => [call.on, call.args]), [
    [null, search],
    ["vps", search],
    [null, ["app-search", "--refresh", "--limit", "0"]],
    ["vps", ["app-search", "--refresh", "--limit", "0"]],
    [null, search],
    ["vps", search],
  ]);
});

test("a refresh that failed says why, even when the search after it answers", async () => {
  const state = app({ answers: { here: (args) => (args.includes("--refresh") ? { code: 0, stdout: JSON.stringify({ rows: [], error: "hc ls failed: no core" }), stderr: "" } : ok([])) } });
  const { view } = await open(state);
  assert.match(view.render().text(), /hc ls failed: no core/);
});

test("a refresh that timed out says so, and the list still searches again", async () => {
  const state = app({
    answers: {
      here: (args) => {
        if (args.includes("--refresh")) throw new Error("exec timed out after 30 s");
        return ok([TERMINAL]);
      },
    },
  });
  const { view } = await open(state);
  assert.match(view.render().text(), /fix login.*refresh failed: exec timed out after 30 s/);
  assert.equal(state.calls.filter((call) => !call.args.includes("--refresh")).length, 2);
});

test("Tab is bound in the mod's own key context and switches to old sessions", async () => {
  const state = app({ answers: { here: ok([]) } });
  const { view, c } = await open(state);
  assert.deepEqual(c.bound, [{ keystroke: "tab", action: "omnisearch_mode", context: "OmniSearch" }]);
  const root = view.render();
  assert.ok(root.calls.some(([name, args]) => name === "key_context" && args[0] === "OmniSearch"));
  const [, [, onMode]] = root.calls.find(([name, args]) => name === "on_action" && args[0] === "omnisearch_mode");
  onMode({ action: "omnisearch_mode" }, c);
  await settle(c);
  assert.ok(state.calls.at(-1).args.includes("--archive"));
});

test("moving down before anything is found still selects the first result once it comes", async () => {
  const state = app({ answers: { here: ok([TERMINAL]) }, agents: [{ address: "t3" }] });
  const view = new OmniSearch();
  const c = cx();
  view.init({}, c);
  key(view, c, "down");
  await settle(c);
  await enter(c);
  assert.deepEqual(state.ran, [["agent.focus", { terminal: "t3" }]]);
});

test("typing searches once the debounce fires", async () => {
  const state = app({ answers: { here: ok([]) } });
  const { view, c } = await open(state);
  await type(view, c, "login  bug");
  assert.deepEqual(state.calls.at(-1).args, ["app-search", "--limit", "40", "--", "login", "bug"]);
});

test("a machine without OmniSearch is left out, and with none installed the mod says how to install it", async () => {
  const state = app({ machines: [{ name: null, layout: "ready" }, { name: "vps", layout: "ready" }], answers: { vps: ok([]) } });
  const { view, c } = await open(state);
  const before = state.calls.length;
  await type(view, c, "x");
  assert.deepEqual(state.calls.slice(before).map((call) => call.on), ["vps"]);

  const none = app({ answers: {} });
  const opened = await open(none);
  assert.match(opened.view.render().text(), /uv tool install git\+https:\/\/github.com\/dmnkf\/herdr-omnisearch/);
});

test("Enter on a remote terminal selects its tab and focuses its pane by machine address", async () => {
  const state = app({
    machines: [{ name: null, layout: "ready" }, { name: "vps", layout: "ready" }],
    answers: { vps: ok([{ kind: "space", space: "sp1", name: "web" }, TERMINAL]) },
    panes: [{ pane: "p1", terminal: "vps/t3", tab: "vps/tb1" }],
  });
  const { view, c } = await open(state);
  assert.match(view.render().text(), /fix login.*claude.*web.*vps.*the login form/);
  await enter(c);
  assert.deepEqual(state.ran, [["tab.select", { id: "vps/tb1" }], ["pane.focus", { terminal: "vps/t3" }]]);
  assert.deepEqual(state.closed, ["o1"]);
});

test("a terminal without a pane is focused as an agent, else peeked", async () => {
  const state = app({ answers: { here: ok([TERMINAL, { ...TERMINAL, address: "t4", title: "helper" }]) }, agents: [{ address: "t3" }] });
  const { view, c } = await open(state);
  await enter(c);
  key(view, c, "down");
  await enter(c);
  assert.deepEqual(state.ran, [["agent.focus", { terminal: "t3" }], ["terminal.peek", { terminal: "t4" }]]);
});

test("a space that matched without its terminals goes to the space", async () => {
  const state = app({ answers: { here: ok([{ kind: "space", space: "sp9", name: "billing" }]) } });
  const { view, c } = await open(state);
  assert.match(view.render().text(), /billing/);
  await enter(c);
  assert.deepEqual(state.ran, [["space.select", { id: "sp9" }]]);
});

test("Tab searches old sessions, and Enter resumes one in the space already open on its folder", async () => {
  const session = { kind: "session", id: "a1", agent: "claude", session_id: "s-1", title: "", space_name: "web", updated: 0, snippet: "user: fix it", folder: "/repo", argv: ["claude", "--resume", "s-1"], address: null };
  const state = app({
    answers: { here: (args) => ok(args.includes("--archive") ? [session] : []) },
    spaces: [{ id: "sp2", machine: null, folder: "/elsewhere", tabs: [{ id: "tb7" }] }],
    panes: [{ pane: "p7", terminal: "t9", tab: "tb7", cwd: "/repo" }],
  });
  const { view, c } = await open(state);
  key(view, c, "tab");
  await settle(c);
  assert.ok(state.calls.at(-1).args.includes("--archive"));
  assert.match(view.render().text(), /web.*claude.*user: fix it/);
  await enter(c);
  assert.deepEqual(state.ran, [["tab.new", { space: "sp2", folder: "/repo", argv: ["claude", "--resume", "s-1"] }]]);
});

test("a session resumes in the space whose folder it was in", async () => {
  const session = { kind: "session", id: "a1", agent: "claude", session_id: "s-1", title: "t", space_name: "web", updated: 0, snippet: "", folder: "/repo", argv: ["claude", "--resume", "s-1"], address: null };
  const state = app({
    answers: { here: (args) => ok(args.includes("--archive") ? [session] : []) },
    spaces: [{ id: "spX", machine: "vps", folder: "/repo", tabs: [] }, { id: "sp1", machine: null, folder: "/repo", tabs: [] }],
  });
  const { view, c } = await open(state);
  key(view, c, "tab");
  await settle(c);
  await enter(c);
  assert.deepEqual(state.ran, [["tab.new", { space: "sp1", folder: "/repo", argv: ["claude", "--resume", "s-1"] }]]);
});

test("a remote session with no open space starts a space on its machine, and a running one is focused", async () => {
  const session = { kind: "session", id: "a1", agent: "codex", session_id: "s-2", title: "deploy", space_name: "ops", updated: 0, snippet: "", folder: "/srv", argv: ["codex", "resume", "s-2"], address: null };
  const state = app({
    machines: [{ name: null, layout: "ready" }, { name: "vps", layout: "ready" }],
    answers: { vps: (args) => ok(args.includes("--archive") ? [session, { ...session, address: "t5" }] : []) },
  });
  const { view, c } = await open(state);
  key(view, c, "tab");
  await settle(c);
  await enter(c);
  key(view, c, "down");
  await enter(c);
  assert.deepEqual(state.ran, [
    ["space.new", { folder: "/srv", argv: ["codex", "resume", "s-2"], machine: "vps" }],
    ["terminal.peek", { terminal: "vps/t5" }],
  ]);
});

test("results of a superseded search never replace newer ones", async () => {
  let release;
  const slow = new Promise((resolve) => (release = resolve));
  const state = app({ answers: { here: (args) => (args.includes("old") ? slow : ok([{ ...TERMINAL, title: "newer" }])) } });
  const { view, c } = await open(state);
  globalThis.__input.text = "old";
  globalThis.__input.fire("change", c);
  c.timers.pop()(c);
  const stale = c.tasks.shift();
  await type(view, c, "new");
  release(ok([{ ...TERMINAL, title: "older" }]));
  await stale;
  const text = view.render().text();
  assert.match(text, /newer/);
  assert.doesNotMatch(text, /older/);
});

test("a session resumes through the agent's launcher on its machine, unless OmniSearch names another program", async () => {
  const session = (agent, argv) => ({ kind: "session", id: agent, agent, session_id: "s-1", title: agent, space_name: "", updated: 0, snippet: "", folder: "/srv", argv, address: null });
  const state = app({
    machines: [{ name: null, layout: "ready" }, { name: "vps", layout: "ready" }],
    answers: { vps: (args) => ok(args.includes("--archive") ? [session("claude", ["claude", "--resume", "s-1"]), session("codex", ["direct-codex", "resume", "s-1"]), session("opencode", ["opencode", "--session", "s-1"])] : []) },
    launchers: {
      vps: [
        { key: "claude", run: ["direct-claude", "--model", "opus"], installed: true },
        { key: "codex", run: ["codex-hub"], installed: true },
        { key: "opencode", run: ["oc"], installed: false },
      ],
    },
  });
  for (let i = 0; i < 3; i++) {
    const { view, c } = await open(state);
    key(view, c, "tab");
    await settle(c);
    for (let down = 0; down < i; down++) key(view, c, "down");
    await enter(c);
  }
  assert.deepEqual(state.ran.map(([, args]) => args.argv), [
    ["direct-claude", "--model", "opus", "--resume", "s-1"],
    ["direct-codex", "resume", "s-1"],
    ["opencode", "--session", "s-1"],
  ]);
});

test("Enter on a session waits for the machine's launchers before starting it", async () => {
  let loaded;
  const session = { kind: "session", id: "c1", agent: "codex", session_id: "s1", title: "t", space_name: "", updated: 0, snippet: "", folder: "/x", argv: ["codex", "resume", "s1"], address: null };
  const state = app({ answers: { here: (args) => ok(args.includes("--archive") ? [session] : []) } });
  state.knownAgents = () => new Promise((resolve) => (loaded = resolve));
  const drain = () => new Promise((resolve) => setTimeout(resolve, 0));
  const view = new OmniSearch();
  const c = cx();
  view.init({}, c);
  await drain();
  key(view, c, "tab");
  await drain();
  globalThis.__input.fire("submit", c);
  await drain();
  assert.deepEqual([state.ran, state.closed], [[], []]);
  loaded([{ key: "codex", run: ["codex-hub"], installed: true }]);
  await drain();
  assert.deepEqual(state.ran, [["space.new", { folder: "/x", argv: ["codex-hub", "resume", "s1"] }]]);
  assert.deepEqual(state.closed, ["o1"]);
});

test("a machine that connects while the popup is open resumes through its own launcher, once however often Enter is pressed", async () => {
  const session = { kind: "session", id: "c1", agent: "codex", session_id: "s1", title: "t", space_name: "", updated: 0, snippet: "", folder: "/x", argv: ["codex", "resume", "s1"], address: null };
  const state = app({
    answers: { here: ok([]), vps: (args) => ok(args.includes("--archive") ? [session] : []) },
    launchers: { here: [], vps: [{ key: "codex", run: ["codex-hub"], installed: true }] },
  });
  const { view, c } = await open(state);
  state.machines = [{ name: null, layout: "ready" }, { name: "vps", layout: "ready" }];
  key(view, c, "tab");
  await settle(c);
  globalThis.__input.fire("submit", c);
  globalThis.__input.fire("submit", c);
  await settle(c);
  assert.deepEqual(state.ran, [["space.new", { folder: "/x", argv: ["codex-hub", "resume", "s1"], machine: "vps" }]]);
});

test("a launcher lookup that never answers falls back to OmniSearch's resume command after a while", async () => {
  const session = { kind: "session", id: "c1", agent: "codex", session_id: "s1", title: "t", space_name: "", updated: 0, snippet: "", folder: "/x", argv: ["codex", "resume", "s1"], address: null };
  const state = app({ answers: { here: (args) => ok(args.includes("--archive") ? [session] : []) } });
  state.knownAgents = () => new Promise(() => {});
  const drain = () => new Promise((resolve) => setTimeout(resolve, 0));
  const view = new OmniSearch();
  const c = cx();
  view.init({}, c);
  await drain();
  key(view, c, "tab");
  await drain();
  globalThis.__input.fire("submit", c);
  await drain();
  assert.deepEqual(state.ran, []);
  c.timers.filter((timer) => timer.ms === 5000).forEach((timer) => timer(c));
  await drain();
  assert.deepEqual(state.ran, [["space.new", { folder: "/x", argv: ["codex", "resume", "s1"] }]]);
});

test("a refresh tells the machine the launchers the app starts its agents with", async () => {
  const state = app({ answers: { here: ok([]) }, launchers: { here: [{ key: "codex", run: ["codex-hub", "--profile", "work"], installed: true }, { key: "pi", run: ["pi"], installed: false }] } });
  await open(state);
  assert.deepEqual(state.calls.find((call) => call.args.includes("--refresh")).args, ["app-search", "--refresh", "--limit", "0", "--launcher", "codex=codex-hub"]);
});

test("a launcher list that came after the wait gave up is used for the next resume", async () => {
  let answer;
  const session = { kind: "session", id: "c1", agent: "codex", session_id: "s1", title: "t", space_name: "", updated: 0, snippet: "", folder: "/x", argv: ["codex", "resume", "s1"], address: null };
  const state = app({ answers: { here: (args) => ok(args.includes("--archive") ? [session] : []) } });
  state.knownAgents = () => new Promise((resolve) => (answer = resolve));
  const drain = () => new Promise((resolve) => setTimeout(resolve, 0));
  const view = new OmniSearch();
  const c = cx();
  view.init({}, c);
  c.timers.filter((timer) => timer.ms === 5000).forEach((timer) => timer(c));
  answer([{ key: "codex", run: ["codex-hub"], installed: true }]);
  await drain();
  key(view, c, "tab");
  await drain();
  globalThis.__input.fire("submit", c);
  await drain();
  assert.deepEqual(state.ran, [["space.new", { folder: "/x", argv: ["codex-hub", "resume", "s1"] }]]);
});

test("a session without a resume command warns instead of starting anything", async () => {
  const session = { kind: "session", id: "a1", agent: "pi", session_id: "s-3", title: "t", space_name: "", updated: 0, snippet: "", folder: "/x", argv: null, address: null };
  const state = app({ answers: { here: (args) => ok(args.includes("--archive") ? [session] : []) } });
  const { view, c } = await open(state);
  key(view, c, "tab");
  await settle(c);
  await enter(c);
  assert.deepEqual(state.ran, []);
  assert.equal(state.notified[0].body, "No resume command for pi");
});
