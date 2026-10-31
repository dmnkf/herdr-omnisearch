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

from herdr_omnisearch import app_search, archive_catalog, cli, herdr_core, live_index, settings, storage  # noqa: E402

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
elif command == "read":
    if rest[1:] != ["--lines", "0", "--json"]:
        sys.exit("only screens may be read: history scrolls a full-screen app")
    print(json.dumps({{"text": state["screens"][rest[0]], "how": "screen", "complete": True}}))
"""

CODEX_SESSION = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0001"


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
        foreground={"pid": 52, "name": "node", "argv": ["node", "/usr/lib/codex/bin.js"], "cwd": "/repo"},
    ),
    terminal("t3", foreground={"pid": 63, "name": "claude", "argv": ["claude"]}),
    terminal("t4", labels={"herdr.space": "sp1", "herdr.tab": "tb1", "where": "web / 1"}, foreground={"name": "bash"}),
    terminal("t5", state="finished", labels={"herdr.space": "sp1", "herdr.tab": "tb1"}, foreground={"name": "claude"}),
    terminal("t7", labels={"herdr.space": "sp1", "herdr.tab": "tb2", "where": "web / 2"}, foreground={"name": "codex"}),
    terminal("vps/t9", labels={"herdr.space": "sp1", "herdr.tab": "tb1", "where": "web / 1"}, foreground={"name": "claude"}),
]
SCREENS = {"t1": "the login form rejects valid emails", "t2": "waiting for approval", "t3": "not in Herdr", "t4": "$ ls",
           "t5": "the login form, finished",
           "vps/t9": "the login form on vps"}


class HerdrCoreTests(unittest.TestCase):
    def core(self, root, terminals=TERMINALS):
        bin_dir = root / "bin"
        bin_dir.mkdir()
        (bin_dir / "hc").write_text(FAKE_HC, encoding="utf-8")
        (bin_dir / "hc").chmod(0o755)
        state = root / "hc-state.json"
        state.write_text(json.dumps({"terminals": terminals, "screens": SCREENS, "vanished": ["t6"]}), encoding="utf-8")
        claude = root / "claude" / "sessions"
        claude.mkdir(parents=True)
        (claude / "41.json").write_text(json.dumps({"pid": 41, "sessionId": "cl-1", "name": "fix-login", "status": "busy"}), encoding="utf-8")
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
            {"kind": "terminal", "address": "t2", "space": "sp1", "tab": "tb1", "title": "Review the login form",
             "agent": "codex", "status": "blocked", "snippet": "waiting for approval", "cwd": "/repo"},
        ]})

    def test_a_terminal_is_found_by_its_session_transcript_and_its_session_resumes_there(self):
        transcript = [
            {"timestamp": "2026-10-27T12:00:00Z", "type": "session_meta", "payload": {"id": CODEX_SESSION, "cwd": "/repo"}},
            {"timestamp": "2026-10-27T12:00:01Z", "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "migrate the walrus schema"}],
            }},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            stack, codex = self.core(Path(tmp))
            with stack:
                (codex / "rollout.jsonl").write_text("".join(json.dumps(line) + "\n" for line in transcript), encoding="utf-8")
                self.app_search("--refresh")
                archive_catalog.archive_catalog_index()
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

    def test_a_resumed_session_is_read_from_the_resume_command(self):
        commands = {
            ("codex", "codex", "resume", CODEX_SESSION): CODEX_SESSION,
            ("codex", "codex", "resume", "-C", "/repo", CODEX_SESSION): CODEX_SESSION,
            ("codex", "codex", "resume", CODEX_SESSION, "--model", "gpt-5"): CODEX_SESSION,
            ("codex", "codex", "-s", "workspace-write", "resume", CODEX_SESSION): CODEX_SESSION,
            ("codex", "codex", "resume", "--last", "-C", "/repo"): "",
            ("codex", "codex", "-s", "workspace-write"): "",
            ("claude", "claude", "--resume", "cl-9"): "cl-9",
            ("claude", "claude", "-r", "cl-9", "--model", "opus"): "cl-9",
            ("claude", "claude", "--resume"): "",
            ("claude", "claude", "resume", CODEX_SESSION): "",
            ("opencode", "opencode", "--session", "ses_123"): "ses_123",
            ("opencode", "opencode", "-s", "ses_123"): "ses_123",
            ("omp", "omp", "--resume", "omp-4"): "omp-4",
        }
        found = {command: herdr_core.resumed_session_id(command[0], list(command[1:])) for command in commands}
        self.assertEqual(found, commands)

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
