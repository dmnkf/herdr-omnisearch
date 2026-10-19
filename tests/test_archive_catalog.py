import gzip
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import archive_catalog, settings, storage, textmatch  # noqa: E402


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

    def test_upgrading_an_old_catalog_keeps_only_searchable_messages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            catalog = root / "state" / "archive-catalog.sqlite3"
            with archive_catalog_env(root) as sessions:
                write_codex_session(
                    sessions / "alpha.jsonl",
                    "alpha",
                    [("user", "penguin migration plan"), ("assistant", "penguin plan approved")],
                )
                write_codex_session(sessions / "beta.jsonl", "beta", [("user", "beta release notes")])
                write_codex_session(sessions / "gone.jsonl", "gone", [("user", "orphaned otter notes")])
                archive_catalog.archive_catalog_index()

                legacy = sqlite3.connect(catalog)
                with legacy:
                    legacy.execute("DELETE FROM catalog_meta WHERE key = 'layout_version'")
                    legacy.execute("UPDATE catalog_sessions SET is_present = 0 WHERE session_key = 'codex:gone'")
                    legacy.execute(
                        """
                        INSERT INTO catalog_messages (
                            message_key, session_key, ordinal, role, content, indexed_generation
                        ) VALUES ('stale-1', 'codex:alpha', 0, 'user', 'obsolete quokka draft', 1)
                        """
                    )
                    legacy.execute(
                        """
                        INSERT INTO catalog_message_fts (rowid, content)
                        SELECT id, content FROM catalog_messages WHERE message_key = 'stale-1'
                        """
                    )
                    legacy.execute(
                        "CREATE VIRTUAL TABLE catalog_fts USING fts5(session_key UNINDEXED, body, tokenize = 'porter unicode61')"
                    )
                    legacy.executemany(
                        "INSERT INTO catalog_fts (session_key, body) VALUES (?, ?)",
                        ((f"codex:old-{index}", "legacy body " * 200) for index in range(300)),
                    )
                    legacy.execute("CREATE TABLE catalog_terms (token TEXT PRIMARY KEY)")
                    legacy.execute("INSERT INTO catalog_terms (token) VALUES ('legacy')")
                    legacy.execute(
                        "CREATE TABLE catalog_token_trigrams (trigram TEXT NOT NULL, token TEXT NOT NULL, PRIMARY KEY (trigram, token))"
                    )
                    legacy.execute("CREATE INDEX idx_catalog_token_trigrams_token ON catalog_token_trigrams(token)")
                    legacy.execute("INSERT INTO catalog_token_trigrams (trigram, token) VALUES ('leg', 'legacy')")
                legacy.close()
                (sessions / "gone.jsonl").unlink()

                queries = ["penguin", "penguim", "beta release", "quokka", "otter", ""]

                def results():
                    return {
                        query: [
                            (row["session_id"], row["content"], row["matched_tokens"])
                            for row in archive_catalog.archive_catalog_search(query, 10)
                        ]
                        for query in queries
                    }

                before = results()
                self.assertEqual(before["penguim"][0][:1], ("alpha",))
                self.assertEqual(before["quokka"], [])
                self.assertEqual(archive_catalog.archive_catalog_index()[:3], (0, 2, 0))

                self.assertEqual(results(), before)
                self.assertEqual(
                    catalog_contents(),
                    (
                        [
                            ("codex:alpha", 0, "penguin migration plan"),
                            ("codex:alpha", 1, "penguin plan approved"),
                            ("codex:beta", 0, "beta release notes"),
                        ],
                        ["approved", "beta", "migration", "notes", "penguin", "plan", "release"],
                    ),
                )
                export = read_absent_export(root)
                self.assertEqual(
                    [
                        (record["session"]["session_key"], [message["content"] for message in record["messages"]])
                        for record in export
                    ],
                    [("codex:gone", ["orphaned otter notes"])],
                )
                conn = storage.archive_catalog_connect()
                try:
                    tables = {
                        row["name"]
                        for row in conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'catalog_%'")
                    }
                    self.assertEqual(
                        sorted(table for table in tables if not table.startswith("catalog_message_fts_")),
                        ["catalog_message_fts", "catalog_message_vocab", "catalog_messages", "catalog_meta", "catalog_sessions"],
                    )
                    self.assertEqual(conn.execute("PRAGMA freelist_count").fetchone()[0], 0)
                    self.assertEqual(
                        conn.execute("SELECT value FROM catalog_meta WHERE key = 'layout_version'").fetchone()[0],
                        "1",
                    )
                finally:
                    conn.close()

                archive_catalog.archive_catalog_index()
                self.assertEqual(len(read_absent_export(root)), 1)

    def test_a_catalog_that_loses_most_of_its_messages_is_vacuumed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with archive_catalog_env(root) as sessions:
                write_codex_session(sessions / "small.jsonl", "small", [("user", "small note")])
                write_codex_session(
                    sessions / "large.jsonl",
                    "large",
                    [("assistant", f"bulky turn {index} " + "padding " * 250) for index in range(200)],
                )
                archive_catalog.archive_catalog_index()
                (sessions / "large.jsonl").unlink()
                archive_catalog.archive_catalog_index()
                conn = storage.archive_catalog_connect()
                try:
                    self.assertEqual(conn.execute("PRAGMA freelist_count").fetchone()[0], 0)
                    self.assertEqual(conn.execute("SELECT COUNT(*) FROM catalog_messages").fetchone()[0], 1)
                finally:
                    conn.close()

    def test_a_search_while_the_indexer_holds_the_write_lock_still_answers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with archive_catalog_env(root) as sessions:
                write_codex_session(sessions / "busy.jsonl", "busy", [("user", "lockstep heron")])
                archive_catalog.archive_catalog_index()
                writer = sqlite3.connect(root / "state" / "archive-catalog.sqlite3", isolation_level=None)
                try:
                    writer.execute("BEGIN IMMEDIATE")
                    writer.execute("DELETE FROM catalog_meta")
                    storage.ARCHIVE_CATALOG_SCHEMA_READY.clear()
                    self.assertEqual(search_ids("heron"), ["busy"])
                finally:
                    writer.execute("ROLLBACK")
                    writer.close()


def clean_text_before_0_12(value):
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", value)
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return value.strip()


def matched_tokens_before_0_12(content, query_terms):
    content_terms = set(textmatch.tokens(content or ""))
    matched = []
    for query_term in query_terms:
        candidates = [
            (textmatch.score_token_candidate(query_term, candidate), candidate)
            for candidate in content_terms
        ]
        candidates = [candidate for candidate in candidates if candidate[0] > 0]
        if candidates:
            matched.append(max(candidates)[1])
    return list(dict.fromkeys(matched))


class ResultRowMatchingTests(unittest.TestCase):
    def test_clean_text_strips_control_characters_but_keeps_text(self):
        cases = {
            "a\x00b\x07c\x1bd\x1f": "abcd",
            "\x1b[31mred\x1b[0m and \x1b[?25lhidden": "red and hidden",
            "line1\r\nline2\rline3\n": "line1\nline2\nline3",
            "col1\tcol2\x0bx\x0cy": "col1\tcol2xy",
            "  café \x85 日本語 \x7f ok  ": "café \x85 日本語 \x7f ok",
        }
        self.assertEqual({value: textmatch.clean_text(value) for value in cases}, cases)

    def test_clean_text_matches_the_previous_implementation(self):
        rng = random.Random(12)
        alphabet = [chr(code) for code in range(0, 33)] + [
            "\x7f", "\x85", "\u2028", "é", "日", "\U0001f600", "a", "Z", "[", "m", "\x1b[", "\x1b[1;32m", "\r\n",
        ]
        values = ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40))) for _ in range(3000)]
        self.assertEqual(
            [textmatch.clean_text(value) for value in values],
            [clean_text_before_0_12(value) for value in values],
        )

    def test_matched_tokens_pick_the_best_token_per_query_term(self):
        cases = [
            ("deploy the api-server now", ["deploy"], ["deploy"]),
            ("plan planning planned", ["plan"], ["plan"]),
            ("the deployment finished", ["deplo"], ["deployment"]),
            ("we deploy daily", ["deployment"], ["deploy"]),
            ("the penguin swims", ["penguim"], ["penguin"]),
            ("abce abcf", ["abcd"], ["abcf"]),
            ("plan for today", ["plan", "plans"], ["plan"]),
            ("the penguin swims", ["zebra"], []),
            ("", ["deploy"], []),
        ]
        self.assertEqual(
            [archive_catalog.archive_catalog_matched_tokens(content, terms) for content, terms, _ in cases],
            [expected for _content, _terms, expected in cases],
        )

    def test_matched_tokens_match_the_previous_implementation(self):
        rng = random.Random(7)
        words = ["deploy", "deployment", "deployed", "plan", "planning", "penguin", "pengiun", "api-server",
                 "abce", "abcf", "abcd", "release", "relase", "ok", "go", "migration", "migrate", "x1"]
        variants = words + ["deplo", "penguim", "plans", "migr", "releasee", "zzzz", "ab", "api", "abc"]
        cases = [
            (
                " ".join(rng.choice(words) for _ in range(rng.randint(0, 12))),
                [rng.choice(variants) for _ in range(rng.randint(1, 4))],
            )
            for _ in range(3000)
        ]
        self.assertEqual(
            [archive_catalog.archive_catalog_matched_tokens(content, terms) for content, terms in cases],
            [matched_tokens_before_0_12(content, terms) for content, terms in cases],
        )


if __name__ == "__main__":
    unittest.main()
