import io
import json
import os
import socket
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import archive_catalog, cli, live_index, machines, navigate, picker, settings  # noqa: E402
from test_herdr_core import FAKE_HC, terminal, write_codex_session  # noqa: E402

TUI_SESSION = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0010"
CORE_SESSION = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0011"
SHOWN = {"herdr.space": "sp1", "herdr.tab": "tb1", "where": "web / 1"}

CORE = [
    terminal("t1", labels=SHOWN, argv=["codex", "resume", CORE_SESSION],
             foreground={"pid": 0, "name": "codex", "argv": ["codex", "resume", CORE_SESSION], "cwd": "/repo"}),
    # The app's view of the TUI's billing pane.
    terminal("t30", labels={**SHOWN, "herdr-mirror.origin": "tui:w2:p1"}, argv=["herdr-mirror", "attach", "tui:w2:p1"],
             foreground={"pid": 0, "name": "herdr-mirror", "argv": ["herdr-mirror", "attach", "tui:w2:p1"], "cwd": "/repo"}),
]
CORE_SCREENS = {"t1": "invoice totals in the app", "t30": "invoice totals mirrored"}

TUI_PANES = [
    {"pane_id": "w2:p1", "workspace_id": "w2", "tab_id": "w2:t1", "label": "billing fix", "agent": "codex",
     "agent_status": "idle", "cwd": "/srv", "agent_session": {"agent": "codex", "value": TUI_SESSION}},
    # The TUI's view of the app's t1.
    {"pane_id": "w2:p2", "workspace_id": "w2", "tab_id": "w2:t1", "label": "", "agent": "codex",
     "agent_status": "idle", "cwd": "/repo"},
]
TUI_SCREENS = {"w2:p1": "invoice totals in the TUI", "w2:p2": "invoice totals in the app"}
TUI_FOREGROUND = {"w2:p1": [["codex", "resume", TUI_SESSION]], "w2:p2": [["herdr-mirror", "attach", "core:t1"]]}


class FakeTui:
    """The herdr TUI server: its snapshot, and what OmniSearch asked of it."""
    calls = []
    focused = ""

    def __init__(self, socket_path=None):
        self.socket_path = socket_path

    def snapshot(self):
        return {"workspaces": [{"workspace_id": "w2", "label": "billing", "active_tab_id": "w2:t1"}],
                "panes": TUI_PANES, "focused_pane_id": FakeTui.focused}

    def pane_read(self, pane_id, lines):
        return TUI_SCREENS[pane_id]

    def pane_foreground(self, pane_id):
        return TUI_FOREGROUND[pane_id]

    def focus_pane(self, pane_id):
        FakeTui.calls.append(("focus", pane_id, self.socket_path))
        FakeTui.focused = pane_id

    def show_notification(self, title, body=None):
        FakeTui.calls.append(("notify", body))

    def create_workspace(self, cwd, label, focus):
        FakeTui.calls.append(("workspace", cwd))
        return {"root_pane": {"pane_id": "w9:p1"}}

    def rename_pane(self, pane_id, label):
        pass

    def send_input(self, pane_id, text):
        FakeTui.calls.append(("type", pane_id, text))


class FakeTuiCli:
    def __init__(self, binary="herdr", env=None):
        pass

    def agent_list(self):
        return []


class FrontendTests(unittest.TestCase):
    def setUp(self):
        FakeTui.calls = []
        FakeTui.focused = ""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        for program in ("hc", "ps"):
            (bin_dir / program).write_text(FAKE_HC, encoding="utf-8")
            (bin_dir / program).chmod(0o755)
        (root / "hc-state.json").write_text(json.dumps({"terminals": CORE, "screens": CORE_SCREENS, "parents": {}}), encoding="utf-8")
        (root / "claude" / "sessions").mkdir(parents=True)
        self.codex = root / "codex"
        self.codex.mkdir()
        config = settings.default_config()
        config["archive_enabled"] = True
        config["archive_agents"] = ["codex"]
        config["archive"]["codex"]["sessions"] = [str(self.codex / "*.jsonl")]
        config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
        config["archive"]["codex"]["launcher"] = "shell"
        stack = ExitStack()
        self.addCleanup(stack.close)
        # A live TUI server is one whose socket accepts connections.
        self.server = stack.enter_context(socket.socket(socket.AF_UNIX))
        self.server.bind(str(root / "herdr.sock"))
        self.server.listen()
        stack.enter_context(patch.dict(os.environ, {
            "PATH": str(bin_dir),
            "HC_FAKE_STATE": str(root / "hc-state.json"),
            "HERDR_OMNISEARCH_DB": str(root / "index.sqlite3"),
            "HERDR_OMNISEARCH_APP_DB": str(root / "app-index.sqlite3"),
            "HERDR_OMNISEARCH_CATALOG_DB": str(root / "archive-catalog.sqlite3"),
            "CLAUDE_CONFIG_DIR": str(root / "claude"),
            "HERDR_SOCKET_PATH": str(root / "herdr.sock"),
        }))
        stack.enter_context(patch.object(settings, "CONFIG_CACHE", config))
        for module, name, fake in ((live_index, "HerdrClient", FakeTui), (navigate, "HerdrClient", FakeTui),
                                   (live_index, "HerdrCLI", FakeTuiCli)):
            stack.enter_context(patch.object(module, name, fake))
        stack.enter_context(patch.object(cli, "watcher_is_running", return_value=True))
        stack.enter_context(patch.object(cli, "socket_is_alive", return_value=True))
        live_index.FACET_CACHE.clear()
        write_codex_session(self.codex / "tui.jsonl", TUI_SESSION, "walrus ledger")
        write_codex_session(self.codex / "core.jsonl", CORE_SESSION, "walrus invoices")
        archive_catalog.archive_catalog_index()

    def both_frontends_indexed(self):
        live_index.index_session(50, False, False)
        return self.app_search("--refresh", "invoice")

    def app_search(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            cli.main(["app-search", *argv])
        return json.loads(out.getvalue())

    def test_the_app_lists_its_terminals_and_the_tuis_panes_but_no_mirror(self):
        answer = self.both_frontends_indexed()
        self.assertEqual(answer, {"error": None, "rows": [
            {"kind": "space", "space": "sp1", "name": "web"},
            {"kind": "terminal", "frontend": "core", "address": "t1", "space": "sp1", "tab": "tb1", "title": "",
             "agent": "codex", "status": "unknown", "snippet": "invoice totals in the app", "cwd": "/repo", "session_id": CORE_SESSION},
            {"kind": "terminal", "frontend": "tui", "address": "w2:p1", "space": "w2", "tab": "w2:t1", "title": "billing fix",
             "agent": "codex", "status": "idle", "snippet": "invoice totals in the TUI", "cwd": "/srv", "session_id": TUI_SESSION, "space_name": "billing",
             "socket": str(self.root / "herdr.sock"), "mirror": "t30"},
        ]})

    def test_the_app_finds_a_session_running_in_the_tui_and_never_starts_it(self):
        self.both_frontends_indexed()
        session = self.app_search("--archive", "ledger")["rows"][0]
        check = self.app_search("--refresh", "--running", f"codex={TUI_SESSION}")
        where = {"kind": "terminal", "frontend": "tui", "address": "w2:p1", "space": "w2", "tab": "w2:t1", "title": "billing fix",
                 "agent": "codex", "space_name": "billing", "socket": str(self.root / "herdr.sock"), "mirror": "t30"}
        self.assertEqual((session["session_id"], session["address"], session["running"]), (TUI_SESSION, "t30", where))
        self.assertEqual(check, {"rows": [where], "error": None})

    def test_a_tui_pane_that_reported_no_session_owns_the_one_its_command_resumes(self):
        session = TUI_PANES[0].pop("agent_session")
        self.addCleanup(TUI_PANES[0].__setitem__, "agent_session", session)
        TUI_FOREGROUND["w2:p3"] = [["rg", TUI_SESSION, "/var/log/agent.log"]]
        TUI_SCREENS["w2:p3"] = "grep output"
        TUI_PANES.append({"pane_id": "w2:p3", "workspace_id": "w2", "tab_id": "w2:t1", "label": "logs", "agent": "", "cwd": "/srv"})
        self.addCleanup(TUI_PANES.pop)
        live_index.index_session(50, False, False)
        check = self.app_search("--running", f"codex={TUI_SESSION}")
        self.assertEqual([(row["frontend"], row["address"]) for row in check["rows"]], [("tui", "w2:p1")])

    def test_a_tui_server_that_exited_runs_nothing_any_more(self):
        self.both_frontends_indexed()
        self.server.close()
        (self.root / "herdr.sock").unlink()
        session = self.app_search("--archive", "ledger")["rows"][0]
        check = self.app_search("--refresh", "--running", f"codex={TUI_SESSION}")
        self.assertEqual((session["address"], session["running"]), (None, None))
        self.assertEqual(check, {"rows": [], "error": None})

    def test_a_second_start_while_the_first_is_starting_is_refused(self):
        self.both_frontends_indexed()
        ended = "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0012"
        first = self.app_search("--running", f"codex={ended}")
        second = self.app_search("--running", f"codex={ended}")
        self.assertEqual(first, {"rows": [], "error": None})
        self.assertEqual(second, {"rows": [], "error": "codex session 0199aaaa is already starting"})

    def test_the_tui_lists_the_apps_terminals_and_focuses_one_through_its_mirror(self):
        self.both_frontends_indexed()
        args = Namespace(limit=10, status=None, agent=None, all_sessions=False, local_only=True)
        rows = picker.picker_rows(args, "invoice")
        core_row = next(row for row in rows if row["pane_id"] == "t1")
        code = picker.picker_focus(args, core_row)
        self.assertEqual([(row["pane_id"], picker.row_title(row, full=True)) for row in rows], [
            ("workspace:w2", "billing"),
            ("w2:p1", "billing · codex · billing fix"),
            ("workspace:sp1", "web · in the app"),
            ("t1", "web · codex · t1 · in the app"),
        ])
        self.assertEqual((code, FakeTui.calls), (0, [("focus", "w2:p2", None)]))

    def test_the_tui_says_where_an_app_terminal_runs_when_nothing_mirrors_it(self):
        TUI_FOREGROUND["w2:p2"] = [["bash"]]
        self.addCleanup(TUI_FOREGROUND.__setitem__, "w2:p2", [["herdr-mirror", "attach", "core:t1"]])
        self.both_frontends_indexed()
        args = Namespace(limit=10, status=None, agent=None, all_sessions=False, local_only=True)
        core_row = next(row for row in picker.picker_rows(args, "invoice") if row["pane_id"] == "t1")
        with redirect_stdout(io.StringIO()) as out:
            code = picker.picker_focus(args, core_row)
        said = "t1 runs in the Herdr app as t1; no Herdr pane mirrors it."
        self.assertEqual((code, out.getvalue(), FakeTui.calls), (0, said + "\n", [("notify", said)]))

    def test_resuming_from_the_tui_focuses_a_session_running_in_the_app_and_starts_one_that_is_not(self):
        live_index.index_session(50, False, False)
        args = Namespace(archive=True, limit=10, agent=None, window_days=14, window_offset=0)
        running = picker.picker_rows(args, "invoices")[0]
        ended = {**running, "session_id": "0199aaaa-bbbb-4ccc-8ddd-eeeeffff0013", "title": "old"}
        with redirect_stdout(io.StringIO()):
            codes = [picker.picker_focus(args, row) for row in (running, ended, ended)]
        self.assertEqual(codes, [0, 0, 0])
        self.assertEqual(FakeTui.calls, [
            ("focus", "w2:p2", None),
            ("workspace", "/repo" if Path("/repo").exists() else str(Path.home())),
            ("type", "w9:p1", f"codex resume -C {Path('/repo') if Path('/repo').exists() else Path.home()} 0199aaaa-bbbb-4ccc-8ddd-eeeeffff0013"),
            ("notify", "codex session 0199aaaa is already starting."),
        ])

    def test_another_machine_sees_both_frontends_and_an_app_terminal_is_focused_there(self):
        self.both_frontends_indexed()
        with redirect_stdout(io.StringIO()) as out:
            cli.cmd_export(None)
        payload = json.loads(out.getvalue())
        machine = {"id": "a" * 32, "label": "vps", "target": "vps", "session": "default"}
        conn = live_index.connect()
        with conn:
            machines.ingest_export(conn, machine, payload)
        conn.close()
        rows = live_index.grouped_search_index("invoice", 10, machines=True)
        synced_core = next(row for row in rows if row["machine_id"] and row["pane_id"] == "t1")
        synced_space = next(row for row in rows if row["machine_id"] and row["pane_id"] == "workspace:sp1")
        with redirect_stdout(io.StringIO()):
            navigate.focus_row(synced_space)
        with patch.object(machines, "saved_machines", return_value=[machine]), patch.object(
            machines, "run_remote", return_value="t1 runs in the Herdr app as t1; no Herdr pane mirrors it.\n"
        ) as remote:
            code = navigate.focus_row(synced_core)
        self.assertEqual(sorted({(row["herdr_session"], row["pane_id"]) for row in payload["docs"]}), [
            ("core:default", "t1"), ("core:default", "workspace:sp1"),
            (live_index.herdr_session_key(), "w2:p1"), (live_index.herdr_session_key(), "workspace:w2"),
        ])
        self.assertEqual(picker.row_title(synced_core, full=True), "vps › web · codex · t1 · in the app")
        self.assertEqual(remote.call_args.args[1], ["focus-target", "--workspace-id", "sp1", "--core", "t1"])
        self.assertEqual((code, FakeTui.calls), (0, [
            ("notify", "web is a space in the Herdr app on vps."),
            ("notify", "vps: t1 runs in the Herdr app as t1; no Herdr pane mirrors it."),
        ]))

    def test_the_tui_searches_old_sessions_on_every_machine_and_resumes_one_on_its_own(self):
        machine = {"id": "b" * 32, "label": "gpubox", "target": "gpubox", "session": "default"}
        remote_row = {"stable_id": "archive-catalog:Z3B1", "agent": "codex", "session_id": "s-remote", "title": "walrus on gpu",
                      "workspace_label": "ml", "source": "archive", "_archive_catalog": True, "content": "walrus"}
        args = Namespace(archive=True, limit=10, agent=None, window_days=14, window_offset=0)
        args.remote_sessions = picker.RemoteSessions(10)
        with patch.object(picker, "saved_machines_or_empty", return_value=[machine]), patch.object(
            machines, "saved_machines", return_value=[machine]
        ), patch.object(machines, "run_remote", side_effect=lambda m, argv, **kw: json.dumps([remote_row]) if argv[0] == "archive-search" else "") as remote:
            picker.cached_picker_rows(args, "walrus", {})
            while args.remote_sessions.waiting("walrus"):
                pass
            rows = picker.cached_picker_rows(args, "walrus", {})
            code = picker.picker_focus(args, rows[-1])
        self.assertEqual([(row.get("machine_label"), row["session_id"]) for row in rows],
                         [(None, CORE_SESSION), (None, TUI_SESSION), ("gpubox", "s-remote")])
        self.assertEqual(picker.row_title(rows[-1]), "gpubox › ml · codex · walrus")
        self.assertEqual([call.args[1] for call in remote.call_args_list],
                         [["archive-search", "--json", "--limit", "10", "--", "walrus"], ["archive-resume", "archive-catalog:Z3B1"]])
        self.assertEqual((code, FakeTui.calls[-1][0]), (0, "notify"))

    def test_a_machine_answering_after_the_rows_were_taken_is_still_shown(self):
        machine = {"id": "c" * 32, "label": "slow", "target": "slow", "session": "default"}
        remote = picker.RemoteSessions(10)
        answered = []
        with patch.object(picker, "saved_machines_or_empty", return_value=[machine]), patch.object(
            picker.threading, "Thread", side_effect=lambda target, args, daemon: answered.append((target, args)) or Mock()
        ):
            before = remote.rows("walrus")
            target, ask_args = answered[0]
            with patch.object(picker, "remote_archive_rows", return_value=[{"stable_id": "archive-catalog:x", "session_id": "s9"}]):
                target(*ask_args)
            waiting = remote.waiting("walrus")
            after = remote.rows("walrus")
        self.assertEqual((before, waiting, [row["session_id"] for row in after], remote.waiting("walrus")), ([], True, ["s9"], False))


if __name__ == "__main__":
    unittest.main()
