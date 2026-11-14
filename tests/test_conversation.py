import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdr_omnisearch.conversation import conversation
from herdr_omnisearch.storage import archive_catalog_connect


def claude_line(role, text, at):
    return json.dumps({"type": role, "timestamp": at, "message": {"role": role, "content": text}})


class ConversationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"HERDR_OMNISEARCH_CATALOG_DB": str(root / "catalog.sqlite3")})
        self.env.start()
        self.history = root / "s1.jsonl"
        self.history.write_text("\n".join([
            claude_line("user", "fix the login form", "2026-11-13T10:00:00Z"),
            claude_line("assistant", "The login form is fixed.", "2026-11-13T10:01:00Z"),
            claude_line("user", "now the signup page", "2026-11-13T10:02:00Z"),
            claude_line("assistant", "Signup works too.", "2026-11-13T10:03:00Z"),
        ]) + "\n")
        conn = archive_catalog_connect()
        conn.execute(
            "INSERT INTO catalog_sessions (session_key, agent, session_id, path, source_size, source_mtime_ns, indexed_at) "
            "VALUES ('claude:s1', 'claude', 's1', ?, 0, 0, 0)",
            (str(self.history),),
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_the_newest_messages_come_from_the_history_file_itself(self):
        found = conversation("claude", "s1", [], limit=2)
        self.assertEqual(
            [(m["role"], m["text"], m["match"]) for m in found["messages"]],
            [("user", "now the signup page", False), ("assistant", "Signup works too.", False)],
        )

    def test_a_search_shows_the_messages_around_the_newest_match(self):
        found = conversation("claude", "s1", ["login"], limit=3)
        self.assertEqual(
            [(m["text"], m["match"]) for m in found["messages"]],
            [("fix the login form", False), ("The login form is fixed.", True), ("now the signup page", False)],
        )

    def test_a_session_the_catalog_does_not_know_is_not_found(self):
        self.assertEqual(conversation("claude", "nope", []), {"found": False, "messages": [], "error": None})


if __name__ == "__main__":
    unittest.main()
