import gzip
import json
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import archive_catalog, settings, storage  # noqa: E402


def write_codex_session(path, session_id, turns):
    records = [{
        "timestamp": "2026-08-04T12:00:00Z",
        "type": "session_meta",
        "payload": {"id": session_id, "cwd": f"/projects/{session_id}"},
    }]
    for second, (role, text) in enumerate(turns, 1):
        records.append({
            "timestamp": f"2026-08-04T12:00:{second:02d}Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": role,
                "content": [{"type": "output_text" if role == "assistant" else "input_text", "text": text}],
            },
        })
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


@contextmanager
def archive_catalog_env(root):
    sessions = root / "sessions"
    sessions.mkdir(exist_ok=True)
    config = settings.default_config()
    config["archive_enabled"] = True
    config["archive_agents"] = ["codex"]
    config["archive"]["codex"]["sessions"] = [str(sessions / "*.jsonl")]
    config["archive"]["codex"]["thread_names"] = str(root / "missing.jsonl")
    environment = {
        "HERDR_PLUGIN_STATE_DIR": str(root / "state"),
        "HERDR_OMNISEARCH_CATALOG_DB": str(root / "state" / "archive-catalog.sqlite3"),
    }
    with patch.dict(os.environ, environment, clear=False), patch.object(settings, "CONFIG_CACHE", config):
        yield sessions


def catalog_contents():
    conn = storage.archive_catalog_connect()
    try:
        messages = [
            (row["session_key"], row["ordinal"], row["content"])
            for row in conn.execute(
                "SELECT session_key, ordinal, content FROM catalog_messages ORDER BY session_key, ordinal"
            )
        ]
        terms = sorted(row["term"] for row in conn.execute("SELECT term FROM catalog_message_vocab"))
        conn.execute("INSERT INTO catalog_message_fts (catalog_message_fts) VALUES ('integrity-check')")
    finally:
        conn.close()
    return messages, terms


def read_absent_export(root):
    with gzip.open(root / "state" / "archive-catalog-absent.jsonl.gz", "rt", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def search_ids(query):
    return [row["session_id"] for row in archive_catalog.archive_catalog_search(query, 10)]


class ArchiveCatalogCompactionTests(unittest.TestCase):
    def test_reindexing_a_changed_session_keeps_only_its_current_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with archive_catalog_env(root) as sessions:
                path = sessions / "edited.jsonl"
                write_codex_session(path, "edited", [("user", "draft walrus plan"), ("assistant", "noted walrus")])
                archive_catalog.archive_catalog_index()
                write_codex_session(path, "edited", [("user", "final penguin plan"), ("assistant", "noted penguin")])
                archive_catalog.archive_catalog_index()

                self.assertEqual(
                    catalog_contents(),
                    (
                        [("codex:edited", 0, "final penguin plan"), ("codex:edited", 1, "noted penguin")],
                        ["final", "noted", "penguin", "plan"],
                    ),
                )
                self.assertEqual(search_ids("walrus"), [])
                self.assertEqual(search_ids("penguin"), ["edited"])

    def test_a_vanished_session_moves_its_messages_to_the_absent_export(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with archive_catalog_env(root) as sessions:
                write_codex_session(sessions / "kept.jsonl", "kept", [("user", "kept giraffe")])
                write_codex_session(
                    sessions / "gone.jsonl",
                    "gone",
                    [("user", "vanishing otter question"), ("assistant", "otter answer")],
                )
                archive_catalog.archive_catalog_index()
                (sessions / "gone.jsonl").unlink()
                self.assertEqual(archive_catalog.archive_catalog_index()[:3], (0, 1, 1))

                self.assertEqual(
                    catalog_contents(),
                    ([("codex:kept", 0, "kept giraffe")], ["giraffe", "kept"]),
                )
                self.assertEqual(search_ids("otter"), [])
                export = read_absent_export(root)
                self.assertEqual(
                    [(record["session"]["session_key"], record["session"]["is_present"]) for record in export],
                    [("codex:gone", 0)],
                )
                self.assertEqual(
                    [(message["ordinal"], message["role"], message["content"]) for message in export[0]["messages"]],
                    [(0, "user", "vanishing otter question"), (1, "assistant", "otter answer")],
                )
                self.assertEqual(
                    oct((root / "state" / "archive-catalog-absent.jsonl.gz").stat().st_mode & 0o777),
                    "0o600",
                )
                conn = storage.archive_catalog_connect()
                try:
                    self.assertEqual(
                        [tuple(row) for row in conn.execute(
                            "SELECT session_key, is_present FROM catalog_sessions ORDER BY session_key"
                        )],
                        [("codex:gone", 0), ("codex:kept", 1)],
                    )
                finally:
                    conn.close()


if __name__ == "__main__":
    unittest.main()
