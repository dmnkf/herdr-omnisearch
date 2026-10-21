import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from herdr_omnisearch import archive_catalog, navigate, settings


class OmpArchiveTests(unittest.TestCase):
    def setUp(self):
        settings.CONFIG_CACHE = None

    def tearDown(self):
        settings.CONFIG_CACHE = None

    def test_default_config_includes_omp(self):
        cfg = settings.default_config()
        self.assertIn("omp", cfg["archive_agents"])
        self.assertIn("omp", cfg["archive"])
        omp_cfg = cfg["archive"]["omp"]
        self.assertEqual(omp_cfg["kind"], "omp")
        self.assertEqual(omp_cfg["launcher"], "agent")
        self.assertEqual(omp_cfg["resume"], "omp --resume {session_id}")

    def test_config_toggling_enabled_false(self):
        ini_content = """
[archive]
enabled = true
agents = codex, claude, opencode, omp

[archive.codex]
enabled = false

[archive.claude]
enabled = false
"""
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(ini_content)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_CONFIG": temp_path}):
                cfg = settings.app_config()
                self.assertNotIn("codex", cfg["archive_agents"])
                self.assertNotIn("claude", cfg["archive_agents"])
                self.assertIn("opencode", cfg["archive_agents"])
                self.assertIn("omp", cfg["archive_agents"])
        finally:
            os.unlink(temp_path)

    def test_config_restricting_to_omp_only(self):
        ini_content = """
[archive]
enabled = true
agents = omp
"""
        with tempfile.NamedTemporaryFile("w", delete=False) as f:
            f.write(ini_content)
            temp_path = f.name
        try:
            with patch.dict(os.environ, {"HERDR_OMNISEARCH_CONFIG": temp_path}):
                cfg = settings.app_config()
                self.assertEqual(cfg["archive_agents"], ["omp"])
        finally:
            os.unlink(temp_path)

    def test_archive_file_metadata_standard_session(self):
        records = [
            {
                "type": "title",
                "v": 1,
                "title": "Investigate OMP Features",
                "source": "auto",
                "updatedAt": "2026-10-06T12:00:00.000Z",
            },
            {
                "type": "session",
                "version": 3,
                "id": "01a11299-0c14-7570-b262-def032d07f72",
                "timestamp": "2026-10-06T11:59:00.000Z",
                "cwd": "/Users/ronnie/project",
                "title": "Investigate OMP Features",
            },
            {
                "type": "message",
                "id": "msg-1",
                "timestamp": "2026-10-06T12:00:10.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Hello assistant"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "2026-10-06T11-59-00-000Z_01a11299-0c14-7570-b262-def032d07f72.jsonl"
            with open(file_path, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")

            meta = archive_catalog.archive_file_metadata("omp", file_path, {})
            self.assertIsNotNone(meta)
            self.assertEqual(meta["agent"], "omp")
            self.assertEqual(meta["session_id"], "01a11299-0c14-7570-b262-def032d07f72")
            self.assertEqual(meta["title"], "Investigate OMP Features")
            self.assertEqual(meta["cwd"], "/Users/ronnie/project")
            self.assertFalse(meta["is_subagent"])

    def test_archive_file_metadata_fallback_title_from_user_message(self):
        records = [
            {
                "type": "session",
                "version": 3,
                "id": "session-xyz",
                "timestamp": "2026-10-06T10:00:00.000Z",
                "cwd": "/tmp",
            },
            {
                "type": "message",
                "id": "msg-1",
                "timestamp": "2026-10-06T10:00:05.000Z",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "Refactor database migrations"}],
                },
            },
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            file_path = Path(tmpdir) / "session-xyz.jsonl"
            with open(file_path, "w") as f:
                for rec in records:
                    f.write(json.dumps(rec) + "\n")

            meta = archive_catalog.archive_file_metadata("omp", file_path, {})
            self.assertIsNotNone(meta)
            self.assertEqual(meta["title"], "Refactor database migrations")
            self.assertEqual(meta["session_id"], "session-xyz")

    def test_archive_file_metadata_detects_subagent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Subagent in a parent folder with timestamp (e.g. parent session folder)
            parent_dir = Path(tmpdir) / "2026-10-06T10-00-00-000Z_parent"
            parent_dir.mkdir()
            subagent_file = parent_dir / "CodeScout.jsonl"
            with open(subagent_file, "w") as f:
                f.write(json.dumps({"type": "session", "id": "sub-1", "cwd": "/tmp"}) + "\n")

            meta = archive_catalog.archive_file_metadata("omp", subagent_file, {})
            self.assertIsNotNone(meta)
            self.assertTrue(meta["is_subagent"])

            # Advisor subagent file
            advisor_file = Path(tmpdir) / "__advisor.jsonl"
            with open(advisor_file, "w") as f:
                f.write(json.dumps({"type": "session", "id": "adv-1", "cwd": "/tmp"}) + "\n")

            meta_adv = archive_catalog.archive_file_metadata("omp", advisor_file, {})
            self.assertIsNotNone(meta_adv)
            self.assertTrue(meta_adv["is_subagent"])

    def test_archive_catalog_turn_parsing(self):
        user_item = {
            "type": "message",
            "timestamp": "2026-10-06T12:00:10.000Z",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Run unit tests"},
                    {"type": "image", "data": "blob:..."},
                ],
            },
        }
        turn = archive_catalog.archive_catalog_turn("omp", user_item)
        self.assertIsNotNone(turn)
        self.assertEqual(turn["role"], "user")
        self.assertEqual(turn["content"], "Run unit tests")
        self.assertEqual(turn["message_at"], "2026-10-06T12:00:10.000Z")

        assistant_item = {
            "type": "message",
            "timestamp": "2026-10-06T12:00:15.000Z",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "All 10 tests passed."}],
            },
        }
        turn_asst = archive_catalog.archive_catalog_turn("omp", assistant_item)
        self.assertIsNotNone(turn_asst)
        self.assertEqual(turn_asst["role"], "assistant")
        self.assertEqual(turn_asst["content"], "All 10 tests passed.")

        # Non-message type ignored
        model_change = {
            "type": "model_change",
            "timestamp": "2026-10-06T12:00:00.000Z",
            "model": "gpt-5",
        }
        self.assertIsNone(archive_catalog.archive_catalog_turn("omp", model_change))

    def test_archive_resume_command_omp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            row = {
                "agent": "omp",
                "session_id": "01a11299-0c14-7570-b262-def032d07f72",
                "cwd": tmpdir,
            }
            cwd, cmd = navigate.archive_resume_command(row)
            self.assertEqual(cwd, tmpdir)
            self.assertEqual(cmd, ["omp", "--resume", "01a11299-0c14-7570-b262-def032d07f72"])

    def test_archive_catalog_search_filters_by_active_agents(self):
        from herdr_omnisearch import storage
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "catalog.sqlite3"
            with patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmpdir}):
                conn = storage.archive_catalog_connect()
                with conn:
                    conn.execute(
                        """
                        INSERT INTO catalog_sessions (
                            session_key, agent, session_id, space_label, title, cwd, path,
                            started_at, updated_at, started_epoch, updated_epoch, preview,
                            is_wrapper, is_present, source_size, source_mtime_ns, indexed_at, message_generation
                        ) VALUES
                        ('s1', 'claude', 'c1', 'Space', 'Claude Session', '/tmp', '/tmp/c1.jsonl',
                         '2026-10-06T12:00:00Z', '2026-10-06T12:00:00Z', 1791315000, 1791315000, 'hello', 0, 1, 10, 10, 10, 1),
                        ('s2', 'omp', 'o1', 'Space', 'OMP Session', '/tmp', '/tmp/o1.jsonl',
                         '2026-10-06T12:00:00Z', '2026-10-06T12:00:00Z', 1791315000, 1791315000, 'hello', 0, 1, 10, 10, 10, 1)
                        """
                    )
                conn.close()

                # With config setting agents = omp, claude should be excluded
                with patch.dict(os.environ, {"HERDR_OMNISEARCH_CONFIG": str(Path(tmpdir) / "empty.ini")}):
                    with patch.dict(settings.app_config(), {"archive_agents": ["omp"]}):
                        results = archive_catalog.archive_catalog_search("", 10)
                        agents = [r["agent"] for r in results]
                        self.assertEqual(agents, ["omp"])

    def test_archive_catalog_index_marks_disabled_agents_not_present(self):
        from herdr_omnisearch import storage
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            with patch.dict(os.environ, {"HERDR_PLUGIN_STATE_DIR": tmpdir}):
                conn = storage.archive_catalog_connect()
                with conn:
                    for agent in ("claude", "omp"):
                        conn.execute(
                            """
                            INSERT INTO catalog_sessions (
                                session_key, agent, session_id, path, source_size,
                                source_mtime_ns, indexed_at, is_present
                            ) VALUES (?, ?, ?, ?, 1, 1, 1, 1)
                            """,
                            (f"{agent}:session", agent, "session", f"/{agent}.jsonl"),
                        )
                conn.close()

                cfg = settings.default_config()
                cfg["archive_enabled"] = True
                cfg["archive_agents"] = ["omp"]
                with patch.object(settings, "CONFIG_CACHE", cfg), patch.object(
                    archive_catalog, "archive_paths", return_value=[Path("/omp.jsonl")]
                ), patch.object(
                    archive_catalog,
                    "archive_file_metadata",
                    return_value={
                        "agent": "omp",
                        "session_id": "session",
                        "title": "Title",
                        "cwd": "/tmp",
                        "path": "/omp.jsonl",
                        "started_at": "2026-10-06T12:00:00Z",
                        "updated_at": "2026-10-06T12:00:00Z",
                        "is_subagent": False,
                    },
                ), patch.object(archive_catalog, "archive_source_items", return_value=[]):
                    archive_catalog.archive_catalog_index()

                conn = storage.archive_catalog_connect()
                try:
                    active = {
                        row[0]
                        for row in conn.execute(
                            "SELECT agent FROM catalog_sessions WHERE is_present = 1"
                        ).fetchall()
                    }
                finally:
                    conn.close()

                self.assertEqual(active, {"omp"})


if __name__ == "__main__":
    unittest.main()
