import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
# Tests purge and rewrite index state; never let that hit the real plugin state dir.
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import (  # noqa: E402
    archive_catalog,
    cli,
    live_index,
    navigate,
    picker,
    settings,
    storage,
    textmatch,
    watcher,
)


class FakeHerdrClient:
    instances = []

    def __init__(self):
        self.reads = []
        self.__class__.instances.append(self)

    def snapshot(self):
        return {
            "version": "0.7.5",
            "protocol": 16,
            "workspaces": [{
                "workspace_id": "w1",
                "active_tab_id": "w1:t1",
                "label": "Project",
                "pane_count": 1,
                "tab_count": 1,
            }],
            "panes": [{
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "pane_id": "w1:p1",
                "terminal_id": "term1",
                "label": "agent",
                "agent": "codex",
                "agent_status": "idle",
                "cwd": "/tmp/project",
            }],
        }

    def pane_read(self, pane_id, lines):
        self.reads.append((pane_id, lines))
        return "indexed live output"


class FakeHerdrCLI:
    instances = []

    def __init__(self, binary="herdr"):
        self.binary = binary
        self.reads = []
        self.focuses = []
        self.starts = []
        self.__class__.instances.append(self)

    def agent_list(self):
        return [{
            "pane_id": "w1:p1",
            "agent": "codex",
            "agent_session": {
                "agent": "claude",
                "kind": "id",
                "value": "session-123",
            },
            "agent_status": "working",
            "foreground_cwd": "/tmp/project",
        }]

    def agent_read(self, pane_id, lines):
        self.reads.append((pane_id, lines))
        return "indexed agent output"

    def agent_focus(self, pane_id):
        self.focuses.append(pane_id)
        return {"type": "agent_focus"}

    def agent_start(self, name, kind, pane_id, agent_args, timeout_ms):
        self.starts.append((name, kind, pane_id, agent_args, timeout_ms))
        return {"type": "agent_start"}


class CliTests(unittest.TestCase):
    def setUp(self):
        FakeHerdrClient.instances.clear()
        FakeHerdrCLI.instances.clear()
        settings.CONFIG_CACHE = None

    def test_manifest_declares_plugin_actions_panes_and_events(self):
        manifest = (ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn('id = "herdr.omnisearch"', manifest)
        self.assertIn('min_herdr_version = "0.7.5"', manifest)
        self.assertIn('id = "live"', manifest)
        self.assertIn('id = "archive"', manifest)
        self.assertIn('id = "open-live"', manifest)
        self.assertIn('on = "pane.created"', manifest)
        self.assertIn('"archive-catalog-start"', manifest)
        self.assertIn('"archive-catalog-index"', manifest)
        self.assertIn('$HERDR_PLUGIN_ROOT/bin/herdr-omnisearch', manifest)
        self.assertEqual(manifest.count("--native"), 2)
        self.assertEqual(manifest.count('placement = "popup"'), 2)
        self.assertNotIn('placement = "overlay"', manifest)

    def test_managed_picker_can_force_native_mode_without_tty_detection(self):
        args = Namespace(
            refresh=False,
            background_refresh=False,
            native=True,
            fzf=False,
        )
        with patch.object(picker.sys.stdin, "isatty", return_value=False), patch.object(
            picker.sys.stdout, "isatty", return_value=False
        ), patch.object(picker.curses, "wrapper", return_value=0) as wrapper, patch.object(
            picker, "fzf_picker"
        ) as fzf_picker:
            self.assertEqual(picker.pick(args), 0)

        wrapper.assert_called_once()
        fzf_picker.assert_not_called()

    def test_managed_archive_picker_refreshes_the_catalog_in_the_background(self):
        args = Namespace(
            refresh=False,
            background_refresh=True,
            native=True,
            fzf=False,
            agents="",
            stale_seconds=300,
            window_days=None,
            window_offset=0,
            verbose=False,
        )
        config = settings.default_config()
        config["archive_enabled"] = True
        with patch.object(settings, "CONFIG_CACHE", config), patch.object(
            picker, "maybe_background_archive_catalog_index"
        ) as background, patch.object(
            picker, "archive_catalog_state", return_value=(4, 1)
        ), patch.object(
            picker.curses, "wrapper", return_value=0
        ):
            self.assertEqual(picker.archive_pick(args), 0)

        background.assert_called_once_with("", 300)

    def test_index_uses_native_agent_identity_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(db)}, clear=False):
                with patch.object(live_index, "HerdrClient", FakeHerdrClient), patch.object(
                    live_index, "HerdrCLI", FakeHerdrCLI
                ):
                    count = live_index.index_session(123, False, False)
            self.assertEqual(count, 2)
            self.assertEqual(FakeHerdrClient.instances[0].reads, [("w1:p1", 123)])
            self.assertEqual(FakeHerdrCLI.instances[0].reads, [])
            conn = sqlite3.connect(db)
            try:
                content = "\n".join(row[0] for row in conn.execute("SELECT content FROM docs"))
                agent, session_id, status = conn.execute(
                    "SELECT agent, agent_session_id, agent_status FROM docs WHERE agent != ''"
                ).fetchone()
            finally:
                conn.close()
            self.assertIn("indexed live output", content)
            self.assertEqual((agent, session_id, status), ("claude", "session-123", "working"))

    def test_pane_text_comes_from_the_socket_read(self):
        client = FakeHerdrClient()
        pane = {"pane_id": "w1:p1", "agent": "codex"}

        text = live_index.pane_recent_text(client, pane, 50)

        self.assertEqual(text, "indexed live output")
        self.assertEqual(client.reads, [("w1:p1", 50)])

    def test_live_search_indexes_reply_only_terminal_text(self):
        class ReplyHerdrClient(FakeHerdrClient):
            def pane_read(self, pane_id, lines):
                self.reads.append((pane_id, lines))
                return "user: Decode the supplied value\nassistant: reply-only-marker"

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(db)}, clear=False):
                with patch.object(live_index, "HerdrClient", ReplyHerdrClient), patch.object(
                    live_index, "HerdrCLI", FakeHerdrCLI
                ):
                    live_index.index_session(500, False, False)
                rows = live_index.search_index("reply-only-marker", 10, all_sessions=True)

        self.assertEqual(len(rows), 1)
        self.assertIn("assistant: reply-only-marker", rows[0]["content"])
        self.assertNotIn("reply-only-marker", rows[0]["content"].splitlines()[0])

    def test_preview_centers_the_last_match_on_agent_output(self):
        content = "\n".join([
            "> Say exactly: ready",
            "prompt detail one",
            "prompt detail two",
            "thinking",
            "assistant: ready",
            "usage",
            "input",
        ])

        lines = picker.preview_lines_for_match(content, ["ready"], 4)

        self.assertIn("assistant: ready", lines)
        self.assertNotIn("> Say exactly: ready", lines)
    def test_socket_pane_read_asks_for_a_format_that_does_not_scroll(self):
        captured = {}

        class RecordingClient(cli.HerdrClient):
            def __init__(self):
                pass

            def request(self, method, params):
                captured["method"] = method
                captured["params"] = params
                return {"read": {"text": "indexed live output"}}

        self.assertEqual(RecordingClient().pane_read("w1:p1", 500), "indexed live output")
        self.assertEqual(captured["method"], "pane.read")
        self.assertEqual(captured["params"]["format"], "ansi")
        self.assertEqual(captured["params"]["source"], "recent_unwrapped")

    def test_agent_focus_uses_native_cli(self):
        row = {"pane_id": "w1:p1", "agent": "codex"}
        with patch.object(navigate, "HerdrCLI", FakeHerdrCLI), patch.object(
            navigate, "focused_pane_id", return_value="w1:p1"
        ):
            self.assertTrue(navigate.focus_exact_pane(row))
        self.assertEqual(FakeHerdrCLI.instances[0].focuses, ["w1:p1"])

    def test_native_archive_start_strips_executable_and_uses_valid_name(self):
        row = {"agent": "codex", "session_id": "019abc1234567890"}
        config = settings.default_config()
        with patch.object(settings, "CONFIG_CACHE", config), patch.object(
            navigate, "HerdrCLI", FakeHerdrCLI
        ):
            navigate.archive_agent_start(
                row,
                "w1:p1",
                ["codex", "resume", "-C", "/tmp/project", row["session_id"]],
            )

        name, kind, pane_id, args, timeout = FakeHerdrCLI.instances[0].starts[0]
        self.assertRegex(name, r"^[a-z][a-z0-9_-]{0,31}$")
        self.assertEqual(kind, "codex")
        self.assertEqual(pane_id, "w1:p1")
        self.assertEqual(args, ["resume", "-C", "/tmp/project", row["session_id"]])
        self.assertEqual(timeout, 60000)

    def test_archive_resume_command_keeps_hostile_values_single_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "space dir; rm -rf ~"
            cwd.mkdir()
            row = {"agent": "codex", "session_id": "019abc", "cwd": str(cwd)}
            config = settings.default_config()
            config["archive"]["codex"]["resume"] = 'codex resume -C "{cwd}" {session_id}'
            with patch.object(settings, "CONFIG_CACHE", config):
                resolved_cwd, command = navigate.archive_resume_command(row)

        self.assertEqual(resolved_cwd, str(cwd))
        self.assertEqual(command, ["codex", "resume", "-C", str(cwd), "019abc"])

    def test_native_archive_start_rejects_wrapper_command(self):
        row = {"agent": "codex", "session_id": "019abc1234567890"}
        config = settings.default_config()
        with patch.object(settings, "CONFIG_CACHE", config):
            with self.assertRaisesRegex(RuntimeError, "launcher = shell"):
                navigate.archive_agent_start(row, "w1:p1", ["hapi", "codex", "resume"])

    def _seed_database(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(path)}, clear=False):
            conn = storage.connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO docs (
                        stable_id, workspace_id, tab_id, pane_id, chunk_index, content, indexed_at
                    )
                    VALUES ('seed:w1:p1:0', 'w1', 'w1:t1', 'w1:p1', 0, 'seed', 0)
                    """
                )
            conn.close()

    def test_db_path_repairs_self_referential_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            db = state / "index.sqlite3"
            db.symlink_to(db)
            Path(str(db) + "-wal").symlink_to(Path(str(db) + "-wal"))
            with patch.dict(
                os.environ,
                {"HERDR_PLUGIN_STATE_DIR": str(state), "XDG_DATA_HOME": str(Path(tmp) / "share")},
                clear=False,
            ):
                os.environ.pop("HERDR_OMNISEARCH_DB", None)
                conn = storage.connect()
                conn.close()
            self.assertTrue(db.is_file())
            self.assertFalse(db.is_symlink())

    def test_db_path_restores_newest_failed_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            backup = state / "index.sqlite3.failed-1700000000"
            self._seed_database(backup)
            db = state / "index.sqlite3"
            db.symlink_to(db)
            with patch.dict(
                os.environ,
                {"HERDR_PLUGIN_STATE_DIR": str(state), "XDG_DATA_HOME": str(Path(tmp) / "share")},
                clear=False,
            ):
                os.environ.pop("HERDR_OMNISEARCH_DB", None)
                path = storage.db_path()
            self.assertTrue(path.is_file())
            self.assertTrue(storage.database_has_index_data(path))
            self.assertFalse(backup.exists())

    def test_concurrent_first_start_migration_is_safe(self):
        # The corruption needs an empty legacy database: with no index data the
        # losing process used to move the fresh database aside and rename the
        # compatibility symlink over it, leaving a self-referential link.
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            share = Path(tmp) / "share"
            legacy = share / "herdr-omnisearch" / "index.sqlite3"
            legacy.parent.mkdir(parents=True, exist_ok=True)
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(legacy)}, clear=False):
                storage.connect().close()
            child_env = {
                key: value
                for key, value in os.environ.items()
                if key != "HERDR_OMNISEARCH_DB"
            }
            child_env["HERDR_PLUGIN_STATE_DIR"] = str(state)
            child_env["XDG_DATA_HOME"] = str(share)
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from herdr_omnisearch import storage; storage.connect().close()"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(SRC)],
                    env=child_env,
                    stderr=subprocess.PIPE,
                )
                for _ in range(12)
            ]
            failures = []
            for worker in workers:
                _stdout, stderr = worker.communicate()
                if worker.returncode != 0:
                    failures.append(stderr)
            self.assertEqual(failures, [])
            db = state / "index.sqlite3"
            self.assertFalse(db.is_symlink())
            self.assertTrue(db.is_file())
            self.assertTrue(legacy.is_symlink())
            # Resolve both sides: on macOS the temp dir sits behind the
            # /var -> /private/var symlink.
            self.assertEqual(os.path.realpath(legacy), os.path.realpath(db))

    def _start_socket_server(self, path):
        # A listener that accepts and drops connections, like a real Herdr
        # session. macOS refuses further connects once un-accepted probe
        # connections fill the backlog, so a bind-only listener is not enough.
        import socket as socket_module
        import threading

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        server = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
        server.bind(str(path))
        server.listen(16)
        server.settimeout(0.05)
        stop = threading.Event()

        def drain():
            while not stop.is_set():
                try:
                    connection, _ = server.accept()
                except OSError:
                    continue
                connection.close()

        thread = threading.Thread(target=drain, daemon=True)
        thread.start()
        self.addCleanup(server.close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(stop.set)
        return server

    def test_two_sessions_share_the_index_without_overwriting(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            sock_a = str(Path(tmp) / "a" / "herdr.sock")
            sock_b = str(Path(tmp) / "b" / "herdr.sock")
            # Both sessions must accept connections or the reaper removes them.
            self._start_socket_server(sock_a)
            self._start_socket_server(sock_b)
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(db)}, clear=False):
                with patch.object(live_index, "HerdrClient", FakeHerdrClient), patch.object(
                    live_index, "HerdrCLI", FakeHerdrCLI
                ):
                    with patch.dict(os.environ, {"HERDR_SOCKET_PATH": sock_a}, clear=False):
                        count_a = live_index.index_session(50, False, False)
                    with patch.dict(os.environ, {"HERDR_SOCKET_PATH": sock_b}, clear=False):
                        live_index.index_session(50, False, False)
                        # Re-indexing one session replaces its own rows only.
                        live_index.index_session(50, False, False)

                conn = sqlite3.connect(db)
                try:
                    sessions = {
                        row[0]: row[1]
                        for row in conn.execute(
                            "SELECT herdr_session, COUNT(*) FROM docs GROUP BY herdr_session"
                        )
                    }
                    total = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
                    distinct_ids = conn.execute(
                        "SELECT COUNT(DISTINCT stable_id) FROM docs"
                    ).fetchone()[0]
                finally:
                    conn.close()

                self.assertEqual(len(sessions), 2)
                self.assertEqual(set(sessions.values()), {count_a})
                self.assertEqual(total, distinct_ids)

                with patch.dict(os.environ, {"HERDR_SOCKET_PATH": sock_a}, clear=False):
                    mine = live_index.search_index("indexed", 20)
                    everything = live_index.search_index("indexed", 20, all_sessions=True)
                self.assertTrue(mine)
                self.assertTrue(all(row["socket_path"] == sock_a for row in mine))
                self.assertEqual(len(everything), len(mine) * 2)

    @staticmethod
    def _seed_session_doc(db, session, socket_path):
        conn = sqlite3.connect(db)
        with conn:
            conn.execute(
                """
                INSERT INTO docs (
                    stable_id, herdr_session, socket_path, workspace_id, tab_id,
                    pane_id, chunk_index, content, indexed_at
                )
                VALUES (?, ?, ?, 'w9', 'w9:t1', 'w9:p1', 0, 'ghost content', 0)
                """,
                (f"{session}-doc", session, socket_path),
            )
        conn.close()

    def test_dead_sessions_are_reaped_on_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            state = Path(tmp) / "state"
            state.mkdir()
            live_sock = Path(tmp) / "live.sock"
            self._start_socket_server(live_sock)
            with patch.dict(
                os.environ,
                {
                    "HERDR_OMNISEARCH_DB": str(db),
                    "HERDR_PLUGIN_STATE_DIR": str(state),
                    "HERDR_SOCKET_PATH": str(Path(tmp) / "current.sock"),
                },
                clear=False,
            ):
                storage.connect().close()
                self._seed_session_doc(db, "dead-11111111", str(Path(tmp) / "gone.sock"))
                self._seed_session_doc(db, "live-22222222", str(live_sock))
                (state / "watch-dead-11111111.log").write_text("old\n", encoding="utf-8")
                with patch.object(live_index, "HerdrClient", FakeHerdrClient), patch.object(
                    live_index, "HerdrCLI", FakeHerdrCLI
                ):
                    live_index.index_session(50, False, False)
                conn = sqlite3.connect(db)
                try:
                    sessions = {
                        row[0]
                        for row in conn.execute("SELECT DISTINCT herdr_session FROM docs")
                    }
                finally:
                    conn.close()
                self.assertNotIn("dead-11111111", sessions)
                self.assertIn("live-22222222", sessions)
                self.assertIn(settings.herdr_session_key(), sessions)
                self.assertFalse((state / "watch-dead-11111111.log").exists())

    def test_watcher_and_index_locks_are_per_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            sock_a = str(Path(tmp) / "a" / "herdr.sock")
            sock_b = str(Path(tmp) / "b" / "herdr.sock")
            with patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": str(state)}, clear=False):
                with patch.dict(os.environ, {"HERDR_SOCKET_PATH": sock_a}, clear=False):
                    pid_a = storage.watcher_pid_path()
                with patch.dict(os.environ, {"HERDR_SOCKET_PATH": sock_b}, clear=False):
                    pid_b = storage.watcher_pid_path()
            self.assertNotEqual(pid_a, pid_b)

    def test_exclusive_lock_is_single_owner_and_follows_child_lifetime(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "index.lock"
            fd = storage.try_exclusive_lock(lock)
            self.assertIsNotNone(fd)
            self.assertTrue(storage.lock_is_held(lock))
            self.assertIsNone(storage.try_exclusive_lock(lock))

            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                pass_fds=(fd,),
            )
            try:
                os.close(fd)
                # The child inherited the descriptor, so the lock survives the
                # parent closing its copy.
                self.assertTrue(storage.lock_is_held(lock))
                self.assertIsNone(storage.try_exclusive_lock(lock))
            finally:
                child.terminate()
                child.wait()
            self.assertFalse(storage.lock_is_held(lock))
            fd = storage.try_exclusive_lock(lock)
            self.assertIsNotNone(fd)
            os.close(fd)

    def test_watcher_liveness_uses_lock_not_pid_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            state.mkdir()
            with patch.dict(
                os.environ, {"HERDR_PLUGIN_STATE_DIR": str(state)}, clear=False
            ):
                # A stale pid file without a lock holder must read as stopped.
                storage.watcher_pid_path().write_text("999999999\n", encoding="utf-8")
                self.assertFalse(watcher.watcher_is_running())
                fd = storage.try_exclusive_lock(storage.watcher_pid_path())
                try:
                    self.assertTrue(watcher.watcher_is_running())
                    self.assertEqual(storage.read_watcher_pid(), os.getpid())
                finally:
                    os.close(fd)
                self.assertFalse(watcher.watcher_is_running())

    def test_archive_launcher_can_be_configured_for_shell_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.ini"
            config.write_text(
                "[archive.codex]\nlauncher = shell\nkind = codex\nstart_timeout_ms = 45000\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"HERDR_OMNISEARCH_CONFIG": str(config)}, clear=False
            ):
                settings.CONFIG_CACHE = None
                parsed = settings.app_config()["archive"]["codex"]
        self.assertEqual(parsed["launcher"], "shell")
        self.assertEqual(parsed["kind"], "codex")
        self.assertEqual(parsed["start_timeout_ms"], 45000)

    def test_shell_archive_launcher_sends_the_full_wrapper_command(self):
        client = Mock()
        row = {"agent": "codex", "session_id": "019abc"}
        config = settings.default_config()
        config["archive"]["codex"]["launcher"] = "shell"
        command = ["hapi", "codex", "resume", "019abc"]

        with patch.object(settings, "CONFIG_CACHE", config):
            navigate.launch_archive_in_pane(client, row, "w1:p1", command)

        client.send_input.assert_called_once_with(
            "w1:p1", "hapi codex resume 019abc"
        )

    def test_watcher_subscribes_to_output_and_status_per_pane(self):
        subscriptions = watcher.watcher_subscriptions({"panes": [{"pane_id": "w1:p1"}]})
        self.assertIn(
            {"type": "pane.scroll_changed", "pane_id": "w1:p1"},
            subscriptions,
        )
        self.assertIn(
            {"type": "pane.agent_status_changed", "pane_id": "w1:p1"},
            subscriptions,
        )

    def test_plugin_paths_take_precedence(self):
        with tempfile.TemporaryDirectory() as config, tempfile.TemporaryDirectory() as state:
            with patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_CONFIG_DIR": config,
                    "HERDR_PLUGIN_STATE_DIR": state,
                },
                clear=False,
            ):
                self.assertEqual(settings.config_dir(), Path(config))
                self.assertEqual(settings.data_dir(), Path(state))

    def test_installer_uses_prefix_defaults_and_accepts_command_key_overrides(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "herdr.toml"
            command = [
                str(ROOT / "install.sh"),
                "--bin-dir",
                str(root / "bin"),
                "--config",
                str(root / "omnisearch.ini"),
                "--herdr-config",
                str(config),
                "--herdr-bin",
                "true",
                "--no-plugin",
            ]
            env = os.environ.copy()
            env["HOME"] = str(root / "home")
            subprocess.run(command, env=env, check=True, capture_output=True, text=True)

            text = config.read_text(encoding="utf-8")
            self.assertIn('key = "prefix+o"', text)
            self.assertIn('key = "prefix+shift+o"', text)

            subprocess.run(
                command + [
                    "--live-key",
                    "cmd+o",
                    "--archive-key",
                    "cmd+shift+o",
                ],
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            text = config.read_text(encoding="utf-8")
            self.assertEqual(text.count("# BEGIN herdr-omnisearch"), 1)
            self.assertIn('key = "cmd+o"', text)
            self.assertIn('key = "cmd+shift+o"', text)
            self.assertNotIn('key = "prefix+o"', text)

    def test_connect_creates_missing_private_database_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "missing" / "state"
            db = state / "index.sqlite3"
            with patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_STATE_DIR": str(state),
                    "HERDR_OMNISEARCH_DB": "",
                },
                clear=False,
            ):
                conn = storage.connect()
                conn.close()

            self.assertTrue(db.is_file())
            self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)

    def test_connect_repairs_database_permissions_before_sqlite_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            db = state / "index.sqlite3"
            state.mkdir()
            db.touch(mode=0o000)
            state.chmod(0o500)
            sqlite_connect = sqlite3.connect

            def connect_after_repair(path, *args, **kwargs):
                self.assertEqual(Path(path).parent.stat().st_mode & 0o777, 0o700)
                self.assertEqual(Path(path).stat().st_mode & 0o777, 0o600)
                return sqlite_connect(path, *args, **kwargs)

            with patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_STATE_DIR": str(state),
                    "HERDR_OMNISEARCH_DB": "",
                },
                clear=False,
            ), patch.object(storage.sqlite3, "connect", side_effect=connect_after_repair):
                conn = storage.connect()
                conn.close()

            self.assertEqual(db.parent.stat().st_mode & 0o777, 0o700)
            self.assertEqual(db.stat().st_mode & 0o777, 0o600)

    def test_connect_rejects_non_file_database_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            db.mkdir()
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(db)}, clear=False):
                with self.assertRaisesRegex(
                    sqlite3.OperationalError,
                    "database path is not a regular file",
                ):
                    storage.connect()

    def test_direct_cli_reuses_installed_plugin_config_and_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugin_config = (
                root / "config" / "herdr" / "plugins" / "config" / "herdr.omnisearch"
            )
            plugin_state = root / "state" / "herdr" / "plugins" / "herdr.omnisearch"
            plugin_config.mkdir(parents=True)
            plugin_state.mkdir(parents=True)

            with patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_CONFIG_DIR": "",
                    "HERDR_PLUGIN_STATE_DIR": "",
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "XDG_STATE_HOME": str(root / "state"),
                    "XDG_DATA_HOME": str(root / "data"),
                },
                clear=False,
            ):
                self.assertEqual(settings.config_dir(), plugin_config)
                self.assertEqual(settings.data_dir(), plugin_state)
                self.assertEqual(storage.db_path(), plugin_state / "index.sqlite3")

    def test_plugin_background_commands_prefer_the_managed_plugin_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "bin" / "herdr-omnisearch"
            command.parent.mkdir()
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            command.chmod(0o755)
            with patch.dict(os.environ, {"HERDR_PLUGIN_ROOT": tmp}, clear=False), patch.object(
                settings.shutil, "which", return_value="/root/.local/bin/herdr-omnisearch"
            ):
                self.assertEqual(settings.cli_command(), [str(command)])

    def test_background_command_has_a_package_safe_module_fallback(self):
        with patch.dict(os.environ, {"HERDR_PLUGIN_ROOT": ""}, clear=False), patch.object(
            settings.shutil, "which", return_value=None
        ), patch.object(settings.sys, "argv", ["/missing/herdr-omnisearch"]):
            self.assertEqual(
                settings.cli_command(),
                [sys.executable, "-m", "herdr_omnisearch"],
            )

    def test_archive_indexing_is_private_and_bounded_by_default(self):
        config = settings.default_config()
        self.assertFalse(config["archive_enabled"])
        self.assertEqual(config["archive_window_days"], 14)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.ini"
            config_path.write_text("[archive]\nenabled = false\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"HERDR_OMNISEARCH_CONFIG": str(config_path)}, clear=False
            ):
                settings.CONFIG_CACHE = None
                with self.assertRaisesRegex(RuntimeError, "archive indexing is disabled"):
                    archive_catalog.archive_catalog_index()

    def test_archive_config_can_explicitly_enable_indexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.ini"
            config.write_text(
                "[archive]\nenabled = true\nwindow_days = 21\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"HERDR_OMNISEARCH_CONFIG": str(config)}, clear=False
            ):
                settings.CONFIG_CACHE = None
                parsed = settings.app_config()
            self.assertTrue(parsed["archive_enabled"])
            self.assertEqual(parsed["archive_window_days"], 21)

    def test_archive_windows_are_calendar_aligned_and_chronological(self):
        now = datetime(2026, 7, 30, 12, 0, 0).timestamp()
        newest_start, newest_end = archive_catalog.archive_window_bounds(14, 0, now=now)
        older_start, older_end = archive_catalog.archive_window_bounds(14, 1, now=now)

        self.assertEqual(datetime.fromtimestamp(newest_start), datetime(2026, 7, 17))
        self.assertEqual(datetime.fromtimestamp(newest_end), datetime(2026, 7, 31))
        self.assertEqual(older_end, newest_start)
        self.assertEqual(older_end - older_start, 14 * 86400)

    def test_archive_record_reader_discards_oversized_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "archive.jsonl"
            path.write_bytes(
                b'{"id":"first"}\n'
                + b'{"content":"'
                + (b"x" * 256)
                + b'"}\n'
                + b'{"id":"last"}\n'
            )

            records = list(archive_catalog.iter_archive_records(path, max_record_bytes=64))

        self.assertEqual([record["id"] for record in records], ["first", "last"])

    def test_archive_metadata_derives_missing_title_from_first_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "rollout-2026-05-04T02-43-03-session.jsonl"
            records = [
                {
                    "timestamp": "2026-05-04T00:43:03Z",
                    "type": "session_meta",
                    "payload": {"id": "session-id", "cwd": "/project"},
                },
                {
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Target migration"}],
                    },
                },
            ]
            path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")

            metadata = archive_catalog.archive_file_metadata("codex", path, {})

        self.assertEqual(metadata["session_id"], "session-id")
        self.assertEqual(metadata["title"], "Target migration")

    def test_archive_picker_searches_all_dates_once_for_nonempty_queries(self):
        args = Namespace(
            agents="",
            agent=None,
            archive=True,
            limit=40,
            status=None,
            window_days=14,
            window_offset=0,
        )
        with patch.object(picker, "archive_catalog_search", return_value=[]) as search:
            picker.picker_rows(args, "target")

        search.assert_called_once_with(
            "target",
            40,
            agent=None,
            window_days=None,
            window_offset=None,
        )

    def test_archive_picker_keeps_window_scope_for_empty_browsing(self):
        args = Namespace(
            agent=None,
            archive=True,
            limit=40,
            status=None,
            window_days=14,
            window_offset=2,
        )
        with patch.object(picker, "archive_catalog_search", return_value=[]) as search:
            picker.picker_rows(args, "")

        search.assert_called_once_with(
            "",
            40,
            agent=None,
            window_days=14,
            window_offset=2,
        )

    def test_archive_catalog_short_terms_are_exact_not_prefixes(self):
        self.assertEqual(
            archive_catalog.archive_catalog_fts_query("a as asa archive"),
            '"a" "as" "asa" "archive"*',
        )

    def test_archive_picker_does_not_query_short_input(self):
        args = Namespace(
            agent=None,
            archive=True,
            limit=40,
            status=None,
            window_days=14,
            window_offset=0,
        )
        with patch.object(picker, "archive_catalog_search") as search:
            self.assertEqual(picker.picker_rows(args, "as"), [])

        search.assert_not_called()
        self.assertEqual(picker.archive_picker_lookup_query(args, "as"), "")
        self.assertEqual(picker.archive_picker_lookup_query(args, "asa"), "asa")

    def test_archive_picker_title_marks_global_search_scope(self):
        args = Namespace(archive=True, window_days=14, window_offset=2)

        self.assertEqual(picker.picker_title(args, "asa"), "Herdr ArchiveSearch | all dates")
        self.assertIn(" to ", picker.picker_title(args, ""))

    def test_pending_keys_uses_idle_timeout_and_restores_blocking(self):
        screen = Mock()
        screen.get_wch.side_effect = ["s", picker.curses.error()]

        self.assertEqual(list(picker.pending_keys(screen, 100)), ["s"])
        self.assertEqual(
            [item.args for item in screen.timeout.call_args_list],
            [(100,), (-1,)],
        )

    def test_archive_catalog_is_incremental_and_searches_all_dates(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "sessions"
            archive_dir.mkdir()
            catalog = root / "state" / "archive-catalog.sqlite3"
            older = archive_dir / "older.jsonl"
            recent = archive_dir / "recent.jsonl"

            def write_session(path, session_id, timestamp, title, content):
                records = [
                    {
                        "timestamp": timestamp,
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "cwd": f"/projects/{session_id}",
                            "timestamp": timestamp,
                        },
                    },
                    {
                        "timestamp": timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": title}],
                        },
                    },
                    {
                        "timestamp": timestamp,
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        },
                    },
                ]
                path.write_text(
                    "".join(json.dumps(record) + "\n" for record in records),
                    encoding="utf-8",
                )

            write_session(
                older,
                "older-session",
                "2026-05-05T12:00:00Z",
                "Target migration",
                "Implemented the ExampleMappingKey mapping.",
            )
            write_session(
                recent,
                "recent-session",
                "2026-08-04T12:00:00Z",
                "Routine maintenance",
                "Updated an unrelated component that references the target once.",
            )
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex"]
            config["archive"]["codex"]["sessions"] = [str(archive_dir / "*.jsonl")]
            config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                "HERDR_OMNISEARCH_CATALOG_DB": str(catalog),
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                settings, "CONFIG_CACHE", config
            ):
                changed, unchanged, removed, _terms = archive_catalog.archive_catalog_index()
                self.assertEqual((changed, unchanged, removed), (2, 0, 0))
                self.assertEqual(archive_catalog.archive_catalog_search("routine", 10)[0]["session_id"], "recent-session")
                exact = archive_catalog.archive_catalog_search("ExampleMappingKey", 10)[0]
                self.assertEqual(exact["session_id"], "older-session")
                self.assertIn("assistant: Implemented the ExampleMappingKey mapping.", exact["content"])
                self.assertIn("assistant: Implemented the ExampleMappingKey", picker.row_title(exact))
                self.assertEqual(
                    archive_catalog.archive_catalog_search("ExampleMappingKez", 10)[0]["session_id"],
                    "older-session",
                )
                self.assertEqual(archive_catalog.archive_catalog_search("older-session", 10), [])
                filtered = archive_catalog.archive_catalog_search("cwd:older-session", 10)
                self.assertEqual(filtered[0]["session_id"], "older-session")
                recent_preview = archive_catalog.archive_catalog_search("", 10)[0]
                self.assertEqual(recent_preview["session_id"], "recent-session")
                self.assertIn("user: Routine maintenance", recent_preview["content"])
                self.assertIn("assistant: Updated an unrelated component", recent_preview["content"])
                self.assertEqual(
                    archive_catalog.archive_catalog_search(
                        "ExampleMappingKey",
                        10,
                        window_days=14,
                        window_offset=0,
                    ),
                    [],
                )
                changed, unchanged, removed, _terms = archive_catalog.archive_catalog_index()
                self.assertEqual((changed, unchanged, removed), (0, 2, 0))

                write_session(
                    older,
                    "older-session",
                    "2026-05-05T12:00:00Z",
                    "Target migration",
                    "Implemented the ExampleMappingKey mapping and final validation.",
                )
                changed, unchanged, removed, _terms = archive_catalog.archive_catalog_index()
                self.assertEqual((changed, unchanged, removed), (1, 1, 0))
                refreshed = archive_catalog.archive_catalog_search("final validation", 10)[0]
                self.assertIn("assistant: Implemented the ExampleMappingKey", refreshed["content"])

                conn = storage.archive_catalog_connect()
                try:
                    self.assertEqual(
                        conn.execute(
                            """
                            SELECT COUNT(*)
                            FROM catalog_messages m
                            JOIN catalog_sessions s ON s.session_key = m.session_key
                            WHERE m.indexed_generation = s.message_generation
                            """
                        ).fetchone()[0],
                        4,
                    )
                finally:
                    conn.close()

    def test_archive_catalog_indexes_only_conversational_message_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "session.jsonl"
            records = [
                {
                    "timestamp": "2026-08-04T12:00:00Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "conversation-session",
                        "cwd": "/projects/metadata-only-marker",
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:01Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "developer",
                        "content": [{"type": "input_text", "text": "private-system-marker"}],
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:02Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "opening-turn-marker"}],
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:03Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Intermediate reply"}],
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:04Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Find the conversation marker"}],
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:05Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "The searchable-answer is here."}],
                    },
                },
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex"]
            config["archive"]["codex"]["sessions"] = [str(path)]
            config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "catalog.sqlite3"),
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                settings, "CONFIG_CACHE", config
            ):
                archive_catalog.archive_catalog_index()
                result = archive_catalog.archive_catalog_search("searchable-answer", 10)[0]
                self.assertIn("assistant: The searchable-answer is here.", result["content"])
                latest = archive_catalog.archive_catalog_search("", 10)[0]
                self.assertNotIn("opening-turn-marker", latest["content"])
                self.assertIn("assistant: Intermediate reply", latest["content"])
                self.assertIn("user: Find the conversation marker", latest["content"])
                self.assertEqual(archive_catalog.archive_catalog_search("private-system-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("metadata-only-marker", 10), [])
                self.assertEqual(
                    archive_catalog.archive_catalog_search("cwd:metadata-only-marker", 10)[0]["session_id"],
                    "conversation-session",
                )

    def test_archive_catalog_searches_and_refreshes_workspace_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "sessions"
            archive_dir.mkdir()

            def write_session(path, session_id, content):
                records = [
                    {
                        "timestamp": "2026-08-04T12:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "id": session_id,
                            "cwd": f"/projects/{session_id}",
                        },
                    },
                    {
                        "timestamp": "2026-08-04T12:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": content}],
                        },
                    },
                ]
                path.write_text(
                    "".join(json.dumps(record) + "\n" for record in records),
                    encoding="utf-8",
                )

            write_session(
                archive_dir / "workspace.jsonl",
                "workspace-session",
                "Completed an unrelated task.",
            )
            write_session(
                archive_dir / "message.jsonl",
                "message-session",
                "A Booking Room phrase appeared in this conversation.",
            )
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex"]
            config["archive"]["codex"]["sessions"] = [str(archive_dir / "*.jsonl")]
            config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "catalog.sqlite3"),
            }
            spaces = {("codex", "workspace-session"): "Booking Room"}
            with patch.dict(os.environ, environment, clear=False), patch.object(
                settings, "CONFIG_CACHE", config
            ), patch.object(
                archive_catalog, "live_space_labels_by_session", return_value=spaces
            ) as session_spaces, patch.object(
                archive_catalog, "live_space_labels_by_cwd", return_value={}
            ):
                self.assertEqual(archive_catalog.archive_catalog_index()[:3], (2, 0, 0))
                results = archive_catalog.archive_catalog_search("booking room", 10)
                self.assertEqual(
                    [row["session_id"] for row in results],
                    ["workspace-session", "message-session"],
                )
                self.assertEqual(results[0]["workspace_label"], "Booking Room")
                self.assertEqual(
                    archive_catalog.archive_catalog_search("booking roon", 10)[0]["session_id"],
                    "workspace-session",
                )

                session_spaces.return_value = {
                    ("codex", "workspace-session"): "Reservations API"
                }
                self.assertEqual(archive_catalog.archive_catalog_index()[:3], (0, 2, 0))
                self.assertEqual(
                    archive_catalog.archive_catalog_search("reservations api", 10)[0]["session_id"],
                    "workspace-session",
                )
                self.assertNotIn(
                    "workspace-session",
                    [row["session_id"] for row in archive_catalog.archive_catalog_search("booking room", 10)],
                )

                session_spaces.return_value = {}
                self.assertEqual(archive_catalog.archive_catalog_index()[:3], (0, 2, 0))
                self.assertEqual(
                    archive_catalog.archive_catalog_search("reservations api", 10)[0]["session_id"],
                    "workspace-session",
                )

    def test_archive_catalog_fuzzy_search_ignores_stale_exact_vocabulary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "session.jsonl"

            def write_answer(answer):
                records = [
                    {
                        "timestamp": "2026-08-04T12:00:00Z",
                        "type": "session_meta",
                        "payload": {"id": "fuzzy-session", "cwd": "/projects/example"},
                    },
                    {
                        "timestamp": "2026-08-04T12:00:01Z",
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": answer}],
                        },
                    },
                ]
                path.write_text(
                    "".join(json.dumps(record) + "\n" for record in records),
                    encoding="utf-8",
                )

            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex"]
            config["archive"]["codex"]["sessions"] = [str(path)]
            config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "catalog.sqlite3"),
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                settings, "CONFIG_CACHE", config
            ):
                write_answer("asaa")
                archive_catalog.archive_catalog_index()
                write_answer("asa")
                archive_catalog.archive_catalog_index()

                result = archive_catalog.archive_catalog_search("asaa", 10)[0]
                self.assertEqual(result["session_id"], "fuzzy-session")
                self.assertEqual(result["matched_tokens"], ["asa"])

    def test_archive_catalog_hides_review_wrappers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "sessions"
            archive_dir.mkdir()
            path = archive_dir / "wrapper.jsonl"
            records = [
                {
                    "timestamp": "2026-08-04T12:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "wrapper-session", "cwd": "/projects/review"},
                },
                {
                    "timestamp": "2026-08-04T12:00:00Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{
                            "type": "input_text",
                            "text": "Assess the exact planned action below: hidden-marker",
                        }],
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex"]
            config["archive"]["codex"]["sessions"] = [str(path)]
            config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
            with patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                    "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "catalog.sqlite3"),
                },
                clear=False,
            ), patch.object(settings, "CONFIG_CACHE", config):
                archive_catalog.archive_catalog_index()
                self.assertEqual(archive_catalog.archive_catalog_search("hidden-marker", 10), [])

    def test_archive_catalog_hides_codex_subagent_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            archive_dir = root / "sessions"
            archive_dir.mkdir()
            path = archive_dir / "guardian.jsonl"
            records = [
                {
                    "timestamp": "2026-09-14T14:17:19Z",
                    "type": "session_meta",
                    "payload": {
                        "id": "guardian-session",
                        "cwd": "/projects/review",
                        "parent_thread_id": "parent-session",
                        "source": {"subagent": {"other": "guardian"}},
                        "thread_source": "guardian_review",
                    },
                },
                {
                    "timestamp": "2026-09-14T14:17:20Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Judge this subagent-marker action"}],
                    },
                },
                {
                    "timestamp": "2026-09-14T14:17:21Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "subagent-marker looks safe"}],
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex"]
            config["archive"]["codex"]["sessions"] = [str(path)]
            config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
            with patch.dict(
                os.environ,
                {
                    "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                    "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "catalog.sqlite3"),
                },
                clear=False,
            ), patch.object(settings, "CONFIG_CACHE", config):
                archive_catalog.archive_catalog_index()
                self.assertEqual(archive_catalog.archive_catalog_search("subagent-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("", 10), [])
                conn = storage.archive_catalog_connect()
                try:
                    row = conn.execute(
                        "SELECT is_wrapper, message_count FROM catalog_sessions WHERE session_id = 'guardian-session'"
                    ).fetchone()
                finally:
                    conn.close()
                self.assertEqual((row["is_wrapper"], row["message_count"]), (1, 2))

    def test_archive_catalog_ignores_reasoning_and_tool_payloads(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "conversation.jsonl"
            records = [
                {
                    "timestamp": "2026-08-04T12:00:00Z",
                    "type": "user",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {"role": "user", "content": "Search the visible response"},
                },
                {
                    "timestamp": "2026-08-04T12:00:01Z",
                    "type": "assistant",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "private-reasoning-marker"},
                            {"type": "text", "text": "The visible-response-marker is indexed."},
                            {
                                "type": "tool_use",
                                "name": "example",
                                "input": {"query": "private-tool-marker"},
                            },
                        ],
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:02Z",
                    "type": "user",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {"role": "user", "content": "[Request interrupted by user]"},
                },
                {
                    "timestamp": "2026-08-04T12:00:03Z",
                    "type": "user",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {
                        "role": "user",
                        "content": "<local-command-stdout>control-marker</local-command-stdout>",
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:04Z",
                    "type": "user",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {"role": "user", "content": "hello"},
                },
                {
                    "timestamp": "2026-08-04T12:00:05Z",
                    "type": "assistant",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {
                        "role": "assistant",
                        "content": "[external_agent_tool_result] legacy-artifact-marker",
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:06Z",
                    "type": "user",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {
                        "role": "user",
                        "content": "<user_shell_command>shell-artifact-marker</user_shell_command>",
                    },
                },
                {
                    "timestamp": "2026-08-04T12:00:07Z",
                    "type": "assistant",
                    "sessionId": "secondary-session",
                    "cwd": "/projects/example",
                    "message": {
                        "role": "assistant",
                        "content": json.dumps(
                            {
                                "risk_level": "low",
                                "user_authorization": "high",
                                "outcome": "allow",
                                "rationale": "approval-artifact-marker",
                            }
                        ),
                    },
                },
            ]
            path.write_text(
                "".join(json.dumps(record) + "\n" for record in records),
                encoding="utf-8",
            )
            trivial = root / "trivial.jsonl"
            trivial.write_text(
                "".join(
                    json.dumps(record) + "\n"
                    for record in [
                        {
                            "timestamp": "2026-08-05T12:00:00Z",
                            "type": "user",
                            "sessionId": "trivial-session",
                            "cwd": "/projects/trivial",
                            "message": {"role": "user", "content": "hello"},
                        }
                    ]
                ),
                encoding="utf-8",
            )
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["claude"]
            config["archive"]["claude"]["sessions"] = [str(root / "*.jsonl")]
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
                "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "catalog.sqlite3"),
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                settings, "CONFIG_CACHE", config
            ):
                archive_catalog.archive_catalog_index()
                result = archive_catalog.archive_catalog_search("visible-response-marker", 10)[0]
                self.assertIn("assistant: The visible-response-marker is indexed.", result["content"])
                latest = archive_catalog.archive_catalog_search("", 10)[0]
                self.assertEqual(len(archive_catalog.archive_catalog_search("", 10)), 1)
                self.assertEqual(latest["session_id"], "secondary-session")
                self.assertIn("assistant: The visible-response-marker is indexed.", latest["content"])
                self.assertNotIn("Request interrupted", latest["content"])
                self.assertNotIn("user: hello", latest["content"])
                self.assertEqual(
                    {row["session_id"] for row in archive_catalog.archive_catalog_search("hello", 10)},
                    {"secondary-session", "trivial-session"},
                )
                self.assertEqual(archive_catalog.archive_catalog_search("private-reasoning-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("private-tool-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("control-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("legacy-artifact-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("shell-artifact-marker", 10), [])
                self.assertEqual(archive_catalog.archive_catalog_search("approval-artifact-marker", 10), [])
                self.assertTrue(archive_catalog.is_archive_noise('{"outcome":"allow"}'))
                for prefix in (
                    "<turn_aborted>",
                    "<task-notification>",
                    "<goal_context>",
                    "<thinking>",
                    "<local-command-stderr>",
                    "<recommended_plugins>",
                    "<bash-input>",
                    "<bash-stdout>",
                    "[routing",
                    "<skill>",
                    "<system-reminder>",
                    "<subagent_notification>",
                ):
                    self.assertTrue(archive_catalog.is_archive_noise(prefix + "control payload"))
                self.assertFalse(archive_catalog.is_archive_noise("<proposed_plan>Keep this useful plan"))

    def test_scoped_archive_catalog_refresh_preserves_other_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = settings.default_config()
            config["archive_enabled"] = True
            config["archive_agents"] = ["codex", "claude"]
            environment = {
                "HERDR_PLUGIN_STATE_DIR": str(root),
                "HERDR_OMNISEARCH_CATALOG_DB": str(root / "catalog.sqlite3"),
            }
            with patch.dict(os.environ, environment, clear=False), patch.object(
                settings, "CONFIG_CACHE", config
            ):
                conn = storage.archive_catalog_connect()
                with conn:
                    for agent in ("codex", "claude"):
                        conn.execute(
                            """
                            INSERT INTO catalog_sessions (
                                session_key, agent, session_id, path, source_size,
                                source_mtime_ns, indexed_at
                            ) VALUES (?, ?, ?, ?, 1, 1, 1)
                            """,
                            (f"{agent}:session", agent, "session", f"/{agent}.jsonl"),
                        )
                conn.close()
                with patch.object(archive_catalog, "archive_paths", return_value=[]):
                    archive_catalog.archive_catalog_index("codex")
                conn = storage.archive_catalog_connect()
                try:
                    active_agents = {
                        row[0]
                        for row in conn.execute(
                            "SELECT agent FROM catalog_sessions WHERE is_present = 1"
                        ).fetchall()
                    }
                    retained_agents = {
                        row[0]
                        for row in conn.execute("SELECT agent FROM catalog_sessions").fetchall()
                    }
                finally:
                    conn.close()

        self.assertEqual(active_agents, {"claude"})
        self.assertEqual(retained_agents, {"codex", "claude"})

    def test_archive_catalog_picker_result_resumes_the_catalog_row(self):
        args = Namespace(archive=True)
        row = {
            "stable_id": "archive-catalog:target",
            "_archive_catalog": True,
            "agent": "codex",
            "session_id": "target-session",
        }
        with patch.object(picker, "focus_archive_row", return_value=0) as focus_row:
            result = picker.picker_focus(args, row)

        self.assertEqual(result, 0)
        focus_row.assert_called_once_with(row)

    def test_purge_requires_confirmation_and_removes_index_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            catalog = Path(tmp) / "archive-catalog.sqlite3"
            db.write_bytes(b"index")
            Path(str(db) + "-wal").write_bytes(b"wal")
            catalog.write_bytes(b"catalog")
            with patch.dict(
                os.environ,
                {
                    "HERDR_OMNISEARCH_DB": str(db),
                    "HERDR_OMNISEARCH_CATALOG_DB": str(catalog),
                },
                clear=False,
            ):
                with patch.object(cli, "cmd_watch_stop", return_value=0):
                    self.assertEqual(cli.cmd_purge(Namespace(yes=False)), 2)
                    self.assertTrue(db.exists())
                    self.assertEqual(cli.cmd_purge(Namespace(yes=True)), 0)
            self.assertFalse(db.exists())
            self.assertFalse(Path(str(db) + "-wal").exists())
            self.assertFalse(catalog.exists())

    def test_plugin_state_migration_moves_legacy_database_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = root / "data" / "herdr-omnisearch" / "index.sqlite3"
            state = root / "state"
            legacy.parent.mkdir(parents=True)
            conn = sqlite3.connect(legacy)
            conn.execute("CREATE TABLE docs (content TEXT)")
            conn.execute("INSERT INTO docs VALUES ('preserved')")
            conn.commit()
            conn.close()
            with patch.dict(
                os.environ,
                {
                    "XDG_DATA_HOME": str(root / "data"),
                    "HERDR_PLUGIN_STATE_DIR": str(state),
                },
                clear=False,
            ):
                os.environ.pop("HERDR_OMNISEARCH_DB", None)
                migrated = storage.db_path()
            self.assertEqual(migrated, state / "index.sqlite3")
            self.assertTrue(legacy.is_symlink())
            # Resolve both sides: on macOS the temp dir sits behind the
            # /var -> /private/var symlink.
            self.assertEqual(legacy.resolve(), migrated.resolve())
            conn = sqlite3.connect(migrated)
            try:
                self.assertEqual(conn.execute("SELECT content FROM docs").fetchone()[0], "preserved")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
