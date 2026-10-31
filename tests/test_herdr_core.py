import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import app_search, archive_catalog, cli, live_index, settings, storage  # noqa: E402

FAKE_HC = f"""#!{sys.executable}
import json, os, sys
state = json.load(open(os.environ["HC_FAKE_STATE"]))
command, *rest = sys.argv[1:]
terminals = {{terminal["address"]: terminal for terminal in state["terminals"]}}
if command == "ls":
    listed = [*terminals, *state.get("vanished", [])]
    print(json.dumps({{"terminals": [{{"address": address, "state": "live"}} for address in listed]}}))
elif command == "get":
    print(json.dumps(terminals[rest[0]]))
elif command == "-o":
    for pid in sys.argv[4].split(","):
        if pid in state["parents"]:
            print(pid, state["parents"][pid])
elif command == "read":
    if rest[1:] != ["--lines", "0", "--json"]:
        sys.exit("only screens may be read: history scrolls a full-screen app")
    print(json.dumps({{"text": state["screens"][rest[0]], "how": "screen", "complete": True}}))
"""

CODEX_SESSION = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0001"
ENDED_SESSION = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0003"
# How the Herdr app starts an agent on a connected machine.
APP_LAUNCH = '"$@"; exec "${SHELL:-sh}" -i'


def write_codex_session(path, session_id, topic):
    transcript = [
        {"timestamp": "2026-10-27T12:00:00Z", "type": "session_meta", "payload": {"id": session_id, "cwd": "/repo"}},
        {"timestamp": "2026-10-27T12:00:01Z", "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": f"migrate the {topic}"}],
        }},
    ]
    path.write_text("".join(json.dumps(line) + "\n" for line in transcript), encoding="utf-8")


def terminal(address, **fields):
    return {"address": address, "state": "live", "labels": {}, "parent": None, "status": None, "title": "", "name": None,
            "cwd": "/repo", "pid": 0, "argv": [], "foreground": {}, **fields}


TERMINALS = [
    terminal(
        "t1",
        labels={"herdr.space": "sp1", "herdr.tab": "tb1", "where": "web / 1"},
        title="✳ Fix the login form",
        pid=41,
        argv=["claude"],
        foreground={"pid": 41, "name": "claude", "argv": ["claude"], "cwd": "/repo"},
    ),
    terminal(
        "t2",
        parent="t1",
        status={"state": "blocked", "title": "Review the login form"},
        argv=["codex", "resume", CODEX_SESSION],
        foreground={"pid": 52, "name": "node", "argv": ["node", "/usr/local/bin/codex", "resume", CODEX_SESSION], "cwd": "/repo"},
    ),
    terminal("t3", foreground={"pid": 63, "name": "claude", "argv": ["claude"]}),
    terminal("t4", labels={"herdr.space": "sp1", "herdr.tab": "tb1", "where": "web / 1"}, foreground={"name": "bash"}),
    terminal("t5", state="finished", labels={"herdr.space": "sp1", "herdr.tab": "tb1"}, status={"state": "idle", "app": "claude-code"}),
    terminal("t7", labels={"herdr.space": "sp1", "herdr.tab": "tb2", "where": "web / 2"}, foreground={"name": "codex"}),
    terminal("vps/t9", labels={"herdr.space": "sp1", "herdr.tab": "tb1", "where": "web / 1"}, status={"state": "idle", "app": "claude-code"}),
    terminal(
        "t10",
        labels={"herdr.space": "sp1", "herdr.tab": "tb5", "where": "web / 5"},
        status={"state": "working", "app": "claude-code"},
        pid=100,
        argv=["sh", "-c", APP_LAUNCH, "sh", "claude"],
        foreground={"pid": 100, "name": "sh", "argv": ["sh", "-c", APP_LAUNCH, "sh", "claude"], "cwd": "/repo"},
    ),
    terminal(
        "t11",
        labels={"herdr.space": "sp1", "herdr.tab": "tb6", "where": "web / 6"},
        pid=110,
        argv=["sh", "-c", APP_LAUNCH, "sh", "codex", "--no-daemon"],
        foreground={"pid": 110, "name": "sh", "argv": ["sh", "-c", APP_LAUNCH, "sh", "codex", "--no-daemon"], "cwd": "/repo"},
    ),
    terminal(
        "t13",
        labels={"herdr.space": "sp1", "herdr.tab": "tb8", "where": "web / 8"},
        argv=["/bin/bash"],
        foreground={"pid": 130, "name": "nvim", "argv": ["nvim", "/root/src/codex"], "cwd": "/repo"},
    ),
    terminal(
        "t12",
        labels={"herdr.space": "sp1", "herdr.tab": "tb7", "where": "web / 7"},
        pid=120,
        argv=["sh", "-c", APP_LAUNCH, "sh", "codex", "resume", ENDED_SESSION],
        foreground={"pid": 120, "name": "bash", "argv": ["/bin/bash", "-i"], "cwd": "/repo"},
    ),
]
SCREENS = {"t1": "the login form rejects valid emails", "t2": "waiting for approval", "t3": "not in Herdr", "t4": "$ ls",
           "t5": "the login form, finished", "t10": "planning the rollout", "t11": "codex ready", "t12": "$ ", "t13": "editing the login form notes",
           "vps/t9": "the login form on vps"}


class HerdrCoreTests(unittest.TestCase):
    def core(self, root, terminals=TERMINALS, screens=None):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for program in ("hc", "ps"):
            (bin_dir / program).write_text(FAKE_HC, encoding="utf-8")
            (bin_dir / program).chmod(0o755)
        state = root / "hc-state.json"
        state.write_text(json.dumps({
            "terminals": terminals, "screens": {**SCREENS, **(screens or {})}, "vanished": ["t6"],
            "parents": {"41": "1", "101": "100", "777": "1"},
        }), encoding="utf-8")
        claude = root / "claude" / "sessions"
        claude.mkdir(parents=True)
        records = [
            {"pid": 41, "sessionId": "cl-1", "name": "fix-login", "status": "busy"},
            {"pid": 101, "sessionId": "cl-2", "name": "rollout-plan", "status": "busy"},
            {"pid": 110, "sessionId": "cl-gone", "name": "ended-long-ago", "status": "idle"},
        ]
        for record in records:
            (claude / f"{record['pid']}.json").write_text(json.dumps(record), encoding="utf-8")
        codex = root / "codex"
        codex.mkdir()
        config = settings.default_config()
        config["archive_enabled"] = True
        config["archive_agents"] = ["codex"]
        config["archive"]["codex"]["sessions"] = [str(codex / "*.jsonl")]
        config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
        stack = ExitStack()
        stack.enter_context(patch.dict(os.environ, {
            "PATH": str(bin_dir),
            "HC_FAKE_STATE": str(state),
            "HERDR_OMNISEARCH_DB": str(root / "index.sqlite3"),
            "HERDR_OMNISEARCH_APP_DB": str(root / "app-index.sqlite3"),
            "HERDR_OMNISEARCH_CATALOG_DB": str(root / "archive-catalog.sqlite3"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
        }))
        stack.enter_context(patch.object(settings, "CONFIG_CACHE", config))
        stack.enter_context(patch.object(app_search, "maybe_background_archive_catalog_index", lambda *args: None))
        return stack, codex

    def app_search(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            cli.main(["app-search", *argv])
        return json.loads(out.getvalue())

    def test_app_search_lists_the_terminals_the_app_shows(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack, _codex = self.core(Path(tmp))
            with stack:
                answer = self.app_search("--refresh")
        self.assertEqual(answer, {"error": None, "rows": [
            {"kind": "space", "space": "sp1", "name": "web"},
            {"kind": "terminal", "address": "t1", "space": "sp1", "tab": "tb1", "title": "Fix the login form",
             "agent": "claude", "status": "working", "snippet": "the login form rejects valid emails", "cwd": "/repo"},
            {"kind": "terminal", "address": "t10", "space": "sp1", "tab": "tb5", "title": "rollout-plan",
             "agent": "claude", "status": "working", "snippet": "planning the rollout", "cwd": "/repo"},
            {"kind": "terminal", "address": "t2", "space": "sp1", "tab": "tb1", "title": "Review the login form",
             "agent": "codex", "status": "blocked", "snippet": "waiting for approval", "cwd": "/repo"},
            {"kind": "terminal", "address": "t11", "space": "sp1", "tab": "tb6", "title": "",
             "agent": "codex", "status": "unknown", "snippet": "codex ready", "cwd": "/repo"},
        ]})

    def test_a_terminal_is_found_by_its_session_transcript_and_its_session_resumes_there(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack, codex = self.core(Path(tmp))
            with stack:
                write_codex_session(codex / "rollout.jsonl", CODEX_SESSION, "walrus schema")
                # Each refresh learns from the catalog, and each catalog run from the refreshed space names.
                self.app_search("--refresh")
                archive_catalog.archive_catalog_index()
                self.app_search("--refresh")
                live = self.app_search("walrus")
                sessions = self.app_search("--archive", "walrus")
        self.assertEqual([(row["kind"], row.get("address"), row.get("snippet")) for row in live["rows"]],
                         [("space", None, None), ("terminal", "t2", "user: migrate the walrus schema")])
        self.assertEqual(len(sessions["rows"]), 1)
        session = sessions["rows"][0]
        self.assertEqual((session["kind"], session["agent"], session["session_id"], session["space_name"], session["address"]),
                         ("session", "codex", CODEX_SESSION, "web", "t2"))
        self.assertEqual(session["argv"][:2], ["codex", "resume"])
        self.assertEqual(session["argv"][-1], CODEX_SESSION)

    def test_programs_are_read_the_way_herdr_reads_them(self):
        commands = {
            "t20": ["uv", "run", "codex"],
            "t21": ["node", "--require", "/root/preload.js", "/usr/local/bin/codex"],
            "t22": ["node", "/opt/codex/bin/codex.js", "resume"],
            "t23": ["sh", "-c", APP_LAUNCH, "sh", "codex"],
            "t24": ["fish", "-c", "command $argv[2..]; exec $argv[1] -i", "fish", "codex"],
            "t25": ["bash", "-lc", 'nvim "$1"; exec bash -i', "bash", "/root/src/codex"],
            "t26": ["uv", "run", "pytest"],
            "t27": ["nvim", "/root/src/codex"],
            "t28": ["uv", "run", "pytest", "tests/codex"],
            "t29": ["sh", "-c", APP_LAUNCH, "sh", "env", "nvim", "/root/src/codex"],
            "t30": ["node", "--inspect", "/opt/codex/bin/Codex.js"],
            "t31": ["-bash", "-ic", APP_LAUNCH, "-bash", "env", "FOO=1", "codex"],
            "t32": ["python3", "-m", "pytest", "/root/src/codex"],
            "t33": ["npx", "vitest", "run", "src/codex"],
            "t34": ["fish", "-lic", "command $argv[2..]; exec $argv[1] -li", "fish", "codex"],
        }
        terminals = [
            terminal(address, labels={"herdr.space": "sp1", "herdr.tab": address, "where": "web / 1"},
                     argv=argv, foreground={"pid": 0, "name": Path(argv[0]).name, "argv": argv, "cwd": "/repo"})
            for address, argv in commands.items()
        ]
        with tempfile.TemporaryDirectory() as tmp:
            stack, _codex = self.core(Path(tmp), terminals, screens={address: "prompt" for address in commands})
            with stack:
                settings.CONFIG_CACHE["archive"]["codex"]["resume"] = "uv run codex resume {session_id}"
                rows = self.app_search("--refresh")["rows"]
        self.assertEqual(sorted((row["address"], row["agent"]) for row in rows if row["kind"] == "terminal"),
                         [("t20", "codex"), ("t21", "codex"), ("t22", "codex"), ("t23", "codex"), ("t24", "codex"),
                          ("t30", "codex"), ("t31", "codex"), ("t34", "codex")])

    def test_an_agent_started_through_the_apps_launcher_is_known_by_that_name(self):
        hub = ["sh", "-c", APP_LAUNCH, "sh", "codex-hub", "--profile", "work"]
        terminals = [terminal("t40", labels={"herdr.space": "sp1", "herdr.tab": "tb40", "where": "web / 1"},
                              argv=hub, foreground={"pid": 400, "name": "sh", "argv": hub, "cwd": "/repo"})]
        with tempfile.TemporaryDirectory() as tmp:
            stack, _codex = self.core(Path(tmp), terminals, screens={"t40": "codex hub ready"})
            with stack:
                unknown = self.app_search("--refresh")["rows"]
                known = self.app_search("--refresh", "--launcher", "codex=codex-hub", "--launcher", "pi=sh")["rows"]
        self.assertEqual(unknown, [])
        self.assertEqual([(row["address"], row["agent"]) for row in known if row["kind"] == "terminal"], [("t40", "codex")])

    def test_a_shell_in_a_resume_command_makes_no_terminal_an_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack, _codex = self.core(Path(tmp))
            with stack:
                settings.CONFIG_CACHE["archive"]["codex"]["resume"] = "bash -lc 'codex resume {session_id}'"
                rows = self.app_search("--refresh")["rows"]
        self.assertEqual([row["address"] for row in rows if row["kind"] == "terminal"], ["t1", "t10", "t2", "t11"])

    def test_an_agent_that_exited_leaves_its_session_to_resume(self):
        with tempfile.TemporaryDirectory() as tmp:
            stack, codex = self.core(Path(tmp))
            with stack:
                write_codex_session(codex / "ended.jsonl", ENDED_SESSION, "narwhal export")
                archive_catalog.archive_catalog_index()
                self.app_search("--refresh")
                live = self.app_search("narwhal")["rows"]
                sessions = self.app_search("--archive", "narwhal")["rows"]
        self.assertEqual([row for row in live if row["kind"] == "terminal"], [])
        self.assertEqual([(row["session_id"], row["address"]) for row in sessions], [(ENDED_SESSION, None)])

    def test_an_ended_core_names_no_catalog_session(self):
        stale = {"workspace_id": "spOld", "tab_id": "tbOld", "pane_id": "t2", "agent": "codex",
                 "agent_session": {"value": CODEX_SESSION}, "agent_status": "idle", "cwd": "/repo"}
        with tempfile.TemporaryDirectory() as tmp:
            stack, codex = self.core(Path(tmp))
            with stack:
                write_codex_session(codex / "rollout.jsonl", CODEX_SESSION, "walrus schema")
                live_index.store_session("core:ended", "", [{"workspace_id": "spOld", "label": "abandoned"}], [stale],
                                         lambda pane: "old screen", include_empty=False, include_wrappers=False,
                                         db=storage.app_db_path())
                archive_catalog.archive_catalog_index()
                self.app_search("--refresh")
                archive_catalog.archive_catalog_index()
                session = self.app_search("--archive", "walrus")["rows"][0]
        self.assertEqual(session["space_name"], "web")

    def test_the_herdr_plugin_and_the_app_keep_separate_indexes(self):
        herdr_pane = {"workspace_id": "w1", "tab_id": "w1:t1", "pane_id": "w1:p1", "agent": "claude", "agent_status": "idle"}
        with tempfile.TemporaryDirectory() as tmp:
            stack, _codex = self.core(Path(tmp))
            with stack:
                live_index.store_session("other-herdr", "", [{"workspace_id": "w1", "label": "api"}], [herdr_pane],
                                         lambda pane: "login retries", include_empty=False, include_wrappers=False)
                self.app_search("--refresh")
                herdr_rows = live_index.grouped_search_index("login", 10, all_sessions=True)
                # Any Herdr plugin version may reap or rebuild its own index.
                storage.reset_index_file()
                app_rows = self.app_search("login")["rows"]
        self.assertEqual([row["pane_id"] for row in herdr_rows], ["workspace:w1", "w1:p1"])
        self.assertEqual([row.get("address") for row in app_rows], [None, "t1", "t2"])

    def test_a_resumed_session_is_found_by_its_id_however_its_command_reads(self):
        wrapped = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0002"
        terminals = [
            *TERMINALS,
            terminal(
                "t8",
                labels={"herdr.space": "sp1", "herdr.tab": "tb3", "where": "web / 3"},
                argv=["direct-codex", "--yolo", "-s", "workspace-write", f"--resume={wrapped}"],
                foreground={"pid": 88, "name": "direct-codex", "argv": ["direct-codex", "--yolo", "-s", "workspace-write", f"--resume={wrapped}"], "cwd": "/repo"},
            ),
            terminal(
                "t9",
                labels={"herdr.space": "sp1", "herdr.tab": "tb4", "where": "web / 4"},
                argv=["my-codex"],
                foreground={"pid": 99, "name": "my-codex", "argv": ["my-codex"], "cwd": "/repo"},
            ),
        ]
        sessions = {CODEX_SESSION: "walrus schema", wrapped: "walrus rollout"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack, codex = self.core(root, terminals, screens={"t8": "busy", "t9": "idle prompt"})
            with stack:
                settings.CONFIG_CACHE["archive"]["codex"]["resume"] = "my-codex resume {session_id}"
                for session_id, text in sessions.items():
                    write_codex_session(codex / f"{session_id}.jsonl", session_id, text)
                archive_catalog.archive_catalog_index()
                self.app_search("--refresh")
                live = self.app_search("walrus")["rows"]
                idle = self.app_search("idle", "prompt")["rows"]
        self.assertEqual([(row["address"], row["agent"], row["snippet"]) for row in live if row["kind"] == "terminal"],
                         [("t2", "codex", "user: migrate the walrus schema"), ("t8", "codex", "user: migrate the walrus rollout")])
        self.assertEqual([(row["address"], row["agent"]) for row in idle if row["kind"] == "terminal"], [("t9", "codex")])

    def test_a_background_refresh_leaves_the_scope_herdr_core_ends(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "scope"
            lock_fd = os.open(Path(tmp) / "lock", os.O_CREAT | os.O_RDWR)
            child = [sys.executable, "-c", "import os, sys; open(sys.argv[1], 'w').write(os.environ.get('HC_SCOPE', 'none'))", str(out)]
            with patch.dict(os.environ, {"HC_SCOPE": "7584995-6b2d"}):
                storage.spawn_locked_background(child, lock_fd)
            deadline = time.monotonic() + 10
            while not (out.exists() and out.read_text()) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertEqual(out.read_text(), "none")

    def test_once_herdr_core_is_gone_its_terminals_go_and_the_app_search_says_why(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stack, _codex = self.core(root)
            with stack:
                before = self.app_search("--refresh", "login")
                (root / "bin" / "hc").unlink()
                after = self.app_search("--refresh", "login")
        self.assertEqual([row.get("address") for row in before["rows"]], [None, "t1", "t2"])
        self.assertEqual(after["rows"], [])
        self.assertTrue(after["error"].startswith("hc ls failed: "), after["error"])


if __name__ == "__main__":
    unittest.main()
