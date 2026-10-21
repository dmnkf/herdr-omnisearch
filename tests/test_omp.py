import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from herdr_omnisearch import archive_catalog, navigate, settings, storage

NOW = 1792584000


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def write_codex_session(path, session_id, started, text):
    write_jsonl(path, [
        {"timestamp": started, "type": "session_meta", "payload": {"id": session_id, "cwd": f"/projects/{session_id}"}},
        {"timestamp": started, "type": "response_item", "payload": {
            "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}],
        }},
        {"timestamp": started, "type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": f"done with {text}"}],
        }},
    ])


def write_omp_session(path, session_id, started, text):
    write_jsonl(path, [
        {"type": "session", "version": 3, "id": session_id, "timestamp": started, "cwd": f"/projects/{session_id}"},
        {"type": "message", "id": "m1", "timestamp": started, "message": {
            "role": "user", "content": [{"type": "text", "text": text}],
        }},
        {"type": "message", "id": "m2", "timestamp": started, "message": {
            "role": "assistant", "content": [{"type": "text", "text": f"done with {text}"}],
        }},
    ])


@contextmanager
def archive_config(root, agents="codex, omp", codex_enabled=True, omp_enabled=True):
    config = root / "config.ini"
    config.write_text(
        f"""
[archive]
enabled = true
agents = {agents}

[archive.codex]
enabled = {str(codex_enabled).lower()}
sessions = {root / "codex" / "*.jsonl"}
thread_names = {root / "missing.jsonl"}

[archive.omp]
enabled = {str(omp_enabled).lower()}
sessions = {root / "omp" / "**" / "*.jsonl"}
""",
        encoding="utf-8",
    )
    environment = {
        "HERDR_OMNISEARCH_CONFIG": str(config),
        "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
        "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "archive-catalog.sqlite3"),
    }
    settings.CONFIG_CACHE = None
    try:
        with patch.dict(os.environ, environment):
            yield
    finally:
        settings.CONFIG_CACHE = None


def catalog_rows():
    conn = storage.archive_catalog_connect()
    try:
        return [
            tuple(row)
            for row in conn.execute(
                """
                SELECT s.session_key, s.is_present, COUNT(m.id)
                FROM catalog_sessions s LEFT JOIN catalog_messages m ON m.session_key = s.session_key
                GROUP BY s.session_key ORDER BY s.session_key
                """
            )
        ]
    finally:
        conn.close()


def absent_exports():
    return sorted(path.name for path in storage.archive_catalog_absent_dir().glob("*"))


def visible_sessions(query=""):
    return [row["session_key"] for row in archive_catalog.archive_catalog_search(query, 10)]


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

    def test_archive_file_metadata_marks_nested_transcripts_as_subagents(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project = Path(tmpdir) / "--work--app--"
            parent = "2026-10-06T10-00-00-000Z_parent"
            transcripts = [
                f"{parent}.jsonl",
                f"{parent}/0-CodeScout.jsonl",
                f"{parent}/0-CodeScout/0-Reviewer.jsonl",
                "__advisor.jsonl",
            ]
            for name in transcripts:
                write_omp_session(project / name, Path(name).stem, "2026-10-06T10:00:00Z", "hello")

            self.assertEqual(
                {
                    name: archive_catalog.archive_file_metadata("omp", project / name, {})["is_subagent"]
                    for name in transcripts
                },
                {
                    f"{parent}.jsonl": False,
                    f"{parent}/0-CodeScout.jsonl": True,
                    f"{parent}/0-CodeScout/0-Reviewer.jsonl": True,
                    "__advisor.jsonl": True,
                },
            )

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

    def test_a_disabled_agent_keeps_its_sessions_and_only_hides_them(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_codex_session(root / "codex" / "one.jsonl", "codex-one", "2026-08-04T12:00:00Z", "codex giraffe")
            write_omp_session(root / "omp" / "one.jsonl", "omp-one", "2026-01-10T12:00:00Z", "omp otter")
            both_indexed = [("codex:codex-one", 1, 2), ("omp:omp-one", 1, 2)]

            with archive_config(root):
                self.assertEqual(archive_catalog.archive_catalog_index(), (2, 0, 0, 4))
                self.assertEqual(catalog_rows(), both_indexed)
                self.assertEqual(visible_sessions(), ["codex:codex-one", "omp:omp-one"])
                self.assertEqual(archive_catalog.archive_catalog_max_window_offset(None, 14, now=NOW), 20)

            with archive_config(root, omp_enabled=False):
                indexed = archive_catalog.archive_catalog_index()
                self.assertEqual((absent_exports(), catalog_rows()), ([], both_indexed))
                self.assertEqual(indexed, (0, 1, 0, 0))
                self.assertEqual(visible_sessions(), ["codex:codex-one"])
                self.assertEqual(visible_sessions("otter"), [])
                self.assertEqual(archive_catalog.archive_catalog_max_window_offset(None, 14, now=NOW), 5)

            with archive_config(root, agents="codex"):
                indexed = archive_catalog.archive_catalog_index()
                self.assertEqual((absent_exports(), catalog_rows()), ([], both_indexed))
                self.assertEqual(indexed, (0, 1, 0, 0))
                self.assertEqual(visible_sessions(), ["codex:codex-one"])

            with archive_config(root):
                self.assertEqual(visible_sessions("otter"), ["omp:omp-one"])
                self.assertEqual(archive_catalog.archive_catalog_max_window_offset(None, 14, now=NOW), 20)
                self.assertEqual(archive_catalog.archive_catalog_index(), (0, 2, 0, 0))
                self.assertEqual(catalog_rows(), both_indexed)

    def test_disabling_every_agent_hides_every_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            write_codex_session(root / "codex" / "one.jsonl", "codex-one", "2026-08-04T12:00:00Z", "codex giraffe")
            with archive_config(root):
                archive_catalog.archive_catalog_index()
            with archive_config(root, codex_enabled=False, omp_enabled=False):
                self.assertEqual(settings.app_config()["archive_agents"], [])
                self.assertEqual(visible_sessions(), [])
                self.assertEqual(visible_sessions("giraffe"), [])
                self.assertEqual(archive_catalog.archive_catalog_max_window_offset(None, 14, now=NOW), 0)
                self.assertEqual(catalog_rows(), [("codex:codex-one", 1, 2)])

if __name__ == "__main__":
    unittest.main()
