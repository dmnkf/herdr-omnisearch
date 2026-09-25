import json
import os
import shlex
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import archive_catalog, navigate, opencode_history, settings  # noqa: E402


def export_payload(user_text, reply_text, padding=0):
    return {
        "info": {"id": "ses_x", "title": "t"},
        "messages": [
            {"info": {"role": "user", "time": {"created": 1790000000000}},
             "parts": [{"type": "text", "text": user_text}, {"type": "file", "url": "x"}]},
            {"info": {"role": "assistant", "time": {"created": 1790000005000}},
             "parts": [
                 {"type": "reasoning", "text": "private chain of thought"},
                 {"type": "tool", "state": {"output": "tool noise " * padding}},
                 {"type": "text", "text": reply_text},
                 {"type": "text", "text": "injected reminder", "synthetic": True},
             ]},
        ],
    }


def make_database(path, columns_first=("id", "project_id", "parent_id")):
    conn = sqlite3.connect(path)
    columns = list(columns_first) + ["slug", "directory", "title", "version", "time_created", "time_updated"]
    conn.execute(f"CREATE TABLE session ({', '.join(columns)})")
    rows = [
        ("ses_root", "p", None, "s", "/work/api", "Fix login flow", "1", 1789000000000, 1789500000000),
        ("ses_untitled", "p", None, "s", "/work/web", "New session - 2026-09-19T12:34:01.563Z", "1", 1789100000000, 1789100000000),
        ("ses_child", "p", "ses_root", "s", "/work/api", "Subagent task", "1", 1789000001000, 1789000002000),
    ]
    names = ["id", "project_id", "parent_id", "slug", "directory", "title", "version", "time_created", "time_updated"]
    for row in rows:
        values = dict(zip(names, row))
        conn.execute(
            f"INSERT INTO session ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",
            [values[column] for column in columns],
        )
    conn.commit()
    conn.close()


class OpenCodeListingTests(unittest.TestCase):
    def test_database_lists_top_level_sessions_in_any_column_order(self):
        for order in (("id", "project_id", "parent_id"), ("parent_id", "id", "project_id")):
            with tempfile.TemporaryDirectory() as tmp:
                database = Path(tmp) / "opencode.db"
                make_database(database, order)
                sessions = {s["session_id"]: s for s in opencode_history.list_sessions({"database": str(database)})}
            self.assertEqual(set(sessions), {"ses_root", "ses_untitled"})
            self.assertEqual(sessions["ses_root"]["title"], "Fix login flow")
            self.assertEqual(sessions["ses_untitled"]["title"], "")
            self.assertEqual(sessions["ses_root"]["cwd"], "/work/api")

    def test_older_json_storage_is_listed_without_children(self):
        with tempfile.TemporaryDirectory() as tmp:
            storage = Path(tmp) / "storage"
            (storage / "session" / "global").mkdir(parents=True)
            for name, extra in (("ses_a", {}), ("ses_child", {"parentID": "ses_a"})):
                (storage / "session" / "global" / f"{name}.json").write_text(json.dumps({
                    "id": name, "title": "Old session", "directory": "/srv",
                    "time": {"created": 1782805141995, "updated": 1782805142046}, **extra,
                }), encoding="utf-8")
            sessions = opencode_history.list_sessions({"database": str(Path(tmp) / "missing.db"), "storage": str(storage)})
        self.assertEqual([s["session_id"] for s in sessions], ["ses_a"])

    def test_export_survives_an_exporter_that_exits_before_a_pipe_drains(self):
        with tempfile.TemporaryDirectory() as tmp:
            dump = Path(tmp) / "export.json"
            dump.write_text(json.dumps(export_payload("find the bug", "it is in auth.py", padding=20000)), encoding="utf-8")
            self.assertGreater(dump.stat().st_size, 2 * 65536)
            # Like OpenCode: one non-blocking write, then exit without draining.
            script = (
                "import os; data = open(%r, 'rb').read(); os.set_blocking(1, False)\n"
                "try:\n    os.write(1, data)\nexcept BlockingIOError:\n    pass\nos._exit(0)\n" % str(dump)
            )
            command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}"
            items = list(opencode_history.export_messages({"export": command}, "ses_x"))
        self.assertEqual([(item["role"], item["text"]) for item in items],
                         [("user", "find the bug"), ("assistant", "it is in auth.py")])

    def test_failed_export_raises_so_the_catalog_retries(self):
        command = f"{shlex.quote(sys.executable)} -c 'import sys; sys.exit(3)'"
        with self.assertRaises(opencode_history.OpenCodeError):
            list(opencode_history.export_messages({"export": command}, "ses_x"))

    def test_unreadable_database_raises_instead_of_listing_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            database = Path(tmp) / "opencode.db"
            database.write_bytes(b"not a database")
            with self.assertRaises(opencode_history.OpenCodeError):
                opencode_history.list_sessions({"database": str(database)})


class OpenCodeCatalogTests(unittest.TestCase):
    def setUp(self):
        settings.CONFIG_CACHE = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        tmp = Path(self.tmp.name)
        make_database(tmp / "opencode.db")
        exports = tmp / "exports"
        exports.mkdir()
        (exports / "ses_root.json").write_text(json.dumps(export_payload("login redirect loops", "cookie domain was wrong")), encoding="utf-8")
        (exports / "ses_untitled.json").write_text(json.dumps(export_payload("Plan the dashboard layout", "three columns")), encoding="utf-8")
        exporter = tmp / "export.py"
        exporter.write_text(f"import sys; sys.stdout.write(open({str(exports)!r} + '/' + sys.argv[1] + '.json').read())\n", encoding="utf-8")
        config = tmp / "config.ini"
        config.write_text(
            "[archive]\nenabled = true\nagents = opencode\n\n[archive.opencode]\n"
            f"database = {tmp / 'opencode.db'}\n"
            f"export = {shlex.quote(sys.executable)} {shlex.quote(str(exporter))} {{session_id}}\n",
            encoding="utf-8",
        )
        env = patch.dict(os.environ, {
            "HERDR_OMNISEARCH_CONFIG": str(config),
            "HERDR_OMNISEARCH_CATALOG_DB": str(tmp / "catalog.sqlite3"),
            "HERDR_OMNISEARCH_DB": str(tmp / "index.sqlite3"),
            "HERDR_PLUGIN_STATE_DIR": str(tmp),
        })
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(lambda: setattr(settings, "CONFIG_CACHE", None))

    def test_sessions_are_indexed_searchable_and_incremental(self):
        changed, unchanged, removed, messages = archive_catalog.archive_catalog_index()
        self.assertEqual((changed, unchanged, removed, messages), (2, 0, 0, 4))
        self.assertEqual(archive_catalog.archive_catalog_index()[:2], (0, 2))

        rows = archive_catalog.archive_catalog_search("cookie domain", 10)
        self.assertEqual([row["session_id"] for row in rows], ["ses_root"])
        self.assertEqual(archive_catalog.archive_catalog_search("chain thought", 10), [])

        titles = {row["session_id"]: row["title"] for row in archive_catalog.archive_catalog_search("", 10, window_days=None)}
        self.assertEqual(titles.get("ses_untitled"), "Plan the dashboard layout")

    def use_exporter(self, source):
        (Path(self.tmp.name) / "export.py").write_text(source, encoding="utf-8")

    def test_failed_export_is_retried_and_keeps_earlier_messages(self):
        exporter = (Path(self.tmp.name) / "export.py").read_text(encoding="utf-8")
        self.use_exporter("import sys; sys.exit(1)\n")
        self.assertEqual(archive_catalog.archive_catalog_index()[:2], (0, 0))
        self.use_exporter(exporter)
        self.assertEqual(archive_catalog.archive_catalog_index()[0], 2)
        self.assertTrue(archive_catalog.archive_catalog_search("cookie domain", 10))

        conn = sqlite3.connect(Path(self.tmp.name) / "opencode.db")
        conn.execute("UPDATE session SET time_updated = time_updated + 1000 WHERE id = 'ses_root'")
        conn.commit()
        conn.close()
        self.use_exporter("import sys; sys.exit(1)\n")
        archive_catalog.archive_catalog_index()
        self.assertTrue(archive_catalog.archive_catalog_search("cookie domain", 10))

    def test_missing_opencode_stops_exporting_for_the_rest_of_the_run(self):
        with patch.object(opencode_history, "export_command", side_effect=opencode_history.OpenCodeUnavailable("gone")) as command:
            archive_catalog.archive_catalog_index()
        self.assertEqual(command.call_count, 1)

    def test_unreadable_database_keeps_sessions_visible(self):
        archive_catalog.archive_catalog_index()
        with patch.object(opencode_history, "list_sessions", side_effect=opencode_history.OpenCodeError("locked")):
            self.assertEqual(archive_catalog.archive_catalog_index()[2], 0)
        self.assertTrue(archive_catalog.archive_catalog_search("cookie domain", 10))

    def test_deleted_sessions_leave_the_catalog(self):
        archive_catalog.archive_catalog_index()
        conn = sqlite3.connect(Path(self.tmp.name) / "opencode.db")
        conn.execute("DELETE FROM session WHERE id = 'ses_root'")
        conn.commit()
        conn.close()
        self.assertEqual(archive_catalog.archive_catalog_index()[2], 1)
        self.assertFalse(archive_catalog.archive_catalog_search("cookie domain", 10))

    def test_resume_starts_opencode_on_the_session(self):
        row = {"agent": "opencode", "session_id": "ses_root", "cwd": self.tmp.name}
        cwd, command = navigate.archive_resume_command(row)
        self.assertEqual(command, ["opencode", "--session", "ses_root"])
        self.assertEqual(cwd, self.tmp.name)


if __name__ == "__main__":
    unittest.main()
