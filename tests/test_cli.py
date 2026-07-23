import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from herdr_omnisearch import cli  # noqa: E402


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
        cli.CONFIG_CACHE = None

    def test_manifest_declares_plugin_actions_panes_and_events(self):
        manifest = (ROOT / "herdr-plugin.toml").read_text(encoding="utf-8")
        self.assertIn('id = "herdr.omnisearch"', manifest)
        self.assertIn('min_herdr_version = "0.7.5"', manifest)
        self.assertIn('id = "live"', manifest)
        self.assertIn('id = "archive"', manifest)
        self.assertIn('id = "open-live"', manifest)
        self.assertIn('on = "pane.created"', manifest)
        self.assertIn('$HERDR_PLUGIN_ROOT/bin/herdr-omnisearch', manifest)
        self.assertEqual(manifest.count("--native"), 2)

    def test_managed_picker_can_force_native_mode_without_tty_detection(self):
        args = Namespace(
            refresh=False,
            background_refresh=False,
            native=True,
            fzf=False,
        )
        with patch.object(cli.sys.stdin, "isatty", return_value=False), patch.object(
            cli.sys.stdout, "isatty", return_value=False
        ), patch.object(cli.curses, "wrapper", return_value=0) as wrapper, patch.object(
            cli, "fzf_picker"
        ) as fzf_picker:
            self.assertEqual(cli.pick(args), 0)

        wrapper.assert_called_once()
        fzf_picker.assert_not_called()

    def test_index_uses_native_agent_identity_and_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(db)}, clear=False):
                with patch.object(cli, "HerdrClient", FakeHerdrClient), patch.object(
                    cli, "HerdrCLI", FakeHerdrCLI
                ):
                    count = cli.index_session(123, False, False)
            self.assertEqual(count, 2)
            self.assertEqual(FakeHerdrClient.instances[0].reads, [])
            self.assertEqual(FakeHerdrCLI.instances[0].reads, [("w1:p1", 123)])
            conn = sqlite3.connect(db)
            try:
                content = "\n".join(row[0] for row in conn.execute("SELECT content FROM docs"))
                agent, session_id, status = conn.execute(
                    "SELECT agent, agent_session_id, agent_status FROM docs WHERE agent != ''"
                ).fetchone()
            finally:
                conn.close()
            self.assertIn("indexed agent output", content)
            self.assertEqual((agent, session_id, status), ("claude", "session-123", "working"))

    def test_agent_read_failure_falls_back_to_raw_pane_read(self):
        class FailingAgentCLI:
            def agent_read(self, _pane_id, _lines):
                raise cli.HerdrCLIError("agent disappeared")

        client = FakeHerdrClient()
        pane = {"pane_id": "w1:p1", "agent": "codex"}

        text = cli.pane_recent_text(client, FailingAgentCLI(), pane, 50)

        self.assertEqual(text, "indexed live output")
        self.assertEqual(client.reads, [("w1:p1", 50)])

    def test_agent_focus_uses_native_cli(self):
        row = {"pane_id": "w1:p1", "agent": "codex"}
        with patch.object(cli, "HerdrCLI", FakeHerdrCLI), patch.object(
            cli, "focused_pane_id", return_value="w1:p1"
        ):
            self.assertTrue(cli.focus_exact_pane(row))
        self.assertEqual(FakeHerdrCLI.instances[0].focuses, ["w1:p1"])

    def test_native_archive_start_strips_executable_and_uses_valid_name(self):
        row = {"agent": "codex", "session_id": "019abc1234567890"}
        config = cli.default_config()
        with patch.object(cli, "app_config", return_value=config), patch.object(
            cli, "HerdrCLI", FakeHerdrCLI
        ):
            cli.archive_agent_start(
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
            config = cli.default_config()
            config["archive"]["codex"]["resume"] = 'codex resume -C "{cwd}" {session_id}'
            with patch.object(cli, "app_config", return_value=config):
                resolved_cwd, command = cli.archive_resume_command(row)

        self.assertEqual(resolved_cwd, str(cwd))
        self.assertEqual(command, ["codex", "resume", "-C", str(cwd), "019abc"])

    def test_native_archive_start_rejects_wrapper_command(self):
        row = {"agent": "codex", "session_id": "019abc1234567890"}
        config = cli.default_config()
        with patch.object(cli, "app_config", return_value=config):
            with self.assertRaisesRegex(RuntimeError, "launcher = shell"):
                cli.archive_agent_start(row, "w1:p1", ["hapi", "codex", "resume"])

    def _seed_database(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(path)}, clear=False):
            conn = cli.connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO archive_sessions (
                        session_key, agent, session_id, space_label, title, cwd, path,
                        started_at, updated_at, indexed_at
                    )
                    VALUES ('codex:s1', 'codex', 's1', '', 'seed', '', '/tmp/s1', '', '', 0)
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
                conn = cli.connect()
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
                path = cli.db_path()
            self.assertTrue(path.is_file())
            self.assertTrue(cli.database_has_index_data(path))
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
                cli.connect().close()
            child_env = {
                key: value
                for key, value in os.environ.items()
                if key != "HERDR_OMNISEARCH_DB"
            }
            child_env["HERDR_PLUGIN_STATE_DIR"] = str(state)
            child_env["XDG_DATA_HOME"] = str(share)
            code = (
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "from herdr_omnisearch import cli; cli.connect().close()"
            )
            workers = [
                subprocess.Popen(
                    [sys.executable, "-c", code, str(SRC)],
                    env=child_env,
                    stderr=subprocess.PIPE,
                )
                for _ in range(12)
            ]
            failures = [worker.communicate()[1] for worker in workers if worker.wait() != 0]
            self.assertEqual(failures, [])
            db = state / "index.sqlite3"
            self.assertFalse(db.is_symlink())
            self.assertTrue(db.is_file())
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(os.path.realpath(legacy), str(db))

    def test_exclusive_lock_is_single_owner_and_follows_child_lifetime(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "index.lock"
            fd = cli.try_exclusive_lock(lock)
            self.assertIsNotNone(fd)
            self.assertTrue(cli.lock_is_held(lock))
            self.assertIsNone(cli.try_exclusive_lock(lock))

            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                pass_fds=(fd,),
            )
            try:
                os.close(fd)
                # The child inherited the descriptor, so the lock survives the
                # parent closing its copy.
                self.assertTrue(cli.lock_is_held(lock))
                self.assertIsNone(cli.try_exclusive_lock(lock))
            finally:
                child.terminate()
                child.wait()
            self.assertFalse(cli.lock_is_held(lock))
            fd = cli.try_exclusive_lock(lock)
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
                cli.watcher_pid_path().write_text("999999999\n", encoding="utf-8")
                self.assertFalse(cli.watcher_is_running())
                fd = cli.try_exclusive_lock(cli.watcher_pid_path())
                try:
                    self.assertTrue(cli.watcher_is_running())
                finally:
                    os.close(fd)
                self.assertFalse(cli.watcher_is_running())

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
                cli.CONFIG_CACHE = None
                parsed = cli.app_config()["archive"]["codex"]
        self.assertEqual(parsed["launcher"], "shell")
        self.assertEqual(parsed["kind"], "codex")
        self.assertEqual(parsed["start_timeout_ms"], 45000)

    def test_shell_archive_launcher_sends_the_full_wrapper_command(self):
        client = Mock()
        row = {"agent": "codex", "session_id": "019abc"}
        config = cli.default_config()
        config["archive"]["codex"]["launcher"] = "shell"
        command = ["hapi", "codex", "resume", "019abc"]

        with patch.object(cli, "app_config", return_value=config):
            cli.launch_archive_in_pane(client, row, "w1:p1", command)

        client.send_input.assert_called_once_with(
            "w1:p1", "hapi codex resume 019abc"
        )

    def test_watcher_subscribes_to_output_and_status_per_pane(self):
        subscriptions = cli.watcher_subscriptions({"panes": [{"pane_id": "w1:p1"}]})
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
                self.assertEqual(cli.config_dir(), Path(config))
                self.assertEqual(cli.data_dir(), Path(state))

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
                conn = cli.connect()
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
            ), patch.object(cli.sqlite3, "connect", side_effect=connect_after_repair):
                conn = cli.connect()
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
                    cli.connect()

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
                self.assertEqual(cli.config_dir(), plugin_config)
                self.assertEqual(cli.data_dir(), plugin_state)
                self.assertEqual(cli.db_path(), plugin_state / "index.sqlite3")

    def test_plugin_background_commands_prefer_the_managed_plugin_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / "bin" / "herdr-omnisearch"
            command.parent.mkdir()
            command.write_text("#!/bin/sh\n", encoding="utf-8")
            command.chmod(0o755)
            with patch.dict(os.environ, {"HERDR_PLUGIN_ROOT": tmp}, clear=False), patch.object(
                cli.shutil, "which", return_value="/root/.local/bin/herdr-omnisearch"
            ):
                self.assertEqual(cli.cli_command(), [str(command)])

    def test_background_command_has_a_package_safe_module_fallback(self):
        with patch.dict(os.environ, {"HERDR_PLUGIN_ROOT": ""}, clear=False), patch.object(
            cli.shutil, "which", return_value=None
        ), patch.object(cli.sys, "argv", ["/missing/herdr-omnisearch"]):
            self.assertEqual(
                cli.cli_command(),
                [sys.executable, "-m", "herdr_omnisearch"],
            )

    def test_archive_indexing_is_private_and_bounded_by_default(self):
        config = cli.default_config()
        self.assertFalse(config["archive_enabled"])
        self.assertEqual(config["archive_max_files"], 500)
        self.assertEqual(config["archive_since_days"], 90)
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.ini"
            config_path.write_text("[archive]\nenabled = false\n", encoding="utf-8")
            with patch.dict(
                os.environ, {"HERDR_OMNISEARCH_CONFIG": str(config_path)}, clear=False
            ):
                cli.CONFIG_CACHE = None
                with self.assertRaisesRegex(RuntimeError, "archive indexing is disabled"):
                    cli.index_archive()

    def test_archive_config_can_explicitly_enable_indexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "config.ini"
            config.write_text(
                "[archive]\nenabled = true\nmax_files = 25\nsince_days = 14\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ, {"HERDR_OMNISEARCH_CONFIG": str(config)}, clear=False
            ):
                cli.CONFIG_CACHE = None
                parsed = cli.app_config()
            self.assertTrue(parsed["archive_enabled"])
            self.assertEqual(parsed["archive_max_files"], 25)
            self.assertEqual(parsed["archive_since_days"], 14)

    def test_purge_requires_confirmation_and_removes_index_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "index.sqlite3"
            db.write_bytes(b"index")
            Path(str(db) + "-wal").write_bytes(b"wal")
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_DB": str(db)}, clear=False):
                with patch.object(cli, "cmd_watch_stop", return_value=0):
                    self.assertEqual(cli.cmd_purge(Namespace(yes=False)), 2)
                    self.assertTrue(db.exists())
                    self.assertEqual(cli.cmd_purge(Namespace(yes=True)), 0)
            self.assertFalse(db.exists())
            self.assertFalse(Path(str(db) + "-wal").exists())

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
                migrated = cli.db_path()
            self.assertEqual(migrated, state / "index.sqlite3")
            self.assertTrue(legacy.is_symlink())
            self.assertEqual(legacy.resolve(), migrated)
            conn = sqlite3.connect(migrated)
            try:
                self.assertEqual(conn.execute("SELECT content FROM docs").fetchone()[0], "preserved")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
