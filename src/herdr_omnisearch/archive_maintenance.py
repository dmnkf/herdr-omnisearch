import os
import shutil
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone

from .settings import ensure_private_directory
from .storage import archive_catalog_absent_dir, archive_catalog_db_path

ARCHIVE_CATALOG_LAYOUT_VERSION = 1
ARCHIVE_CATALOG_LEGACY_TABLES = ("catalog_fts", "catalog_terms", "catalog_token_trigrams")
ARCHIVE_CATALOG_UPGRADE_HEADROOM_BYTES = 64 * 1024 * 1024
SQLITE_SECURE_DELETE_MODES = {0: "OFF", 1: "ON", 2: "FAST"}

ABSENT_MESSAGES = "session_key IN (SELECT session_key FROM catalog_sessions WHERE is_present = 0)"
SUPERSEDED_MESSAGES = """
    id IN (
        SELECT m.id
        FROM catalog_messages m
        JOIN catalog_sessions s ON s.session_key = m.session_key
        WHERE m.indexed_generation != s.message_generation AND s.is_present IS NOT 0
    )
"""


def archive_catalog_delete_messages(conn, condition: str, params=()) -> None:
    # External-content FTS keeps a row's terms until told the exact text it indexed.
    conn.execute(
        f"""
        INSERT INTO catalog_message_fts (catalog_message_fts, rowid, content)
        SELECT 'delete', id, content FROM catalog_messages WHERE {condition}
        """,
        params,
    )
    conn.execute(f"DELETE FROM catalog_messages WHERE {condition}", params)


def archive_catalog_sessions_with_messages(conn, session_keys):
    return [
        session_key
        for session_key in session_keys
        if conn.execute(
            "SELECT 1 FROM catalog_messages WHERE session_key = ? LIMIT 1",
            (session_key,),
        ).fetchone()
    ]


def archive_catalog_export_sessions(conn, session_keys):
    import gzip
    import json
    if not session_keys:
        return None
    directory = ensure_private_directory(archive_catalog_absent_dir(), "archive catalog export")
    for leftover in directory.glob("*.tmp"):
        leftover.unlink(missing_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = directory / f"{stamp}-{os.getpid()}.jsonl.gz"
    partial = path.with_name(path.name + ".tmp")
    fd = os.open(partial, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as raw:
            with gzip.open(raw, "wt", encoding="utf-8") as handle:
                for session_key in session_keys:
                    session = conn.execute(
                        "SELECT * FROM catalog_sessions WHERE session_key = ?",
                        (session_key,),
                    ).fetchone()
                    messages = conn.execute(
                        """
                        SELECT message_key, ordinal, role, message_at, message_epoch, content,
                               indexed_generation
                        FROM catalog_messages
                        WHERE session_key = ?
                        ORDER BY indexed_generation ASC, ordinal ASC
                        """,
                        (session_key,),
                    ).fetchall()
                    record = {
                        "session": dict(session),
                        "messages": [dict(message) for message in messages],
                    }
                    handle.write(json.dumps(record) + "\n")
            raw.flush()
            os.fsync(raw.fileno())
        os.rename(partial, path)
        directory_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise
    return path


@contextmanager
def archive_catalog_absent_transaction(conn):
    export = None
    session_keys = []
    try:
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            session_keys = [
                row[0]
                for row in conn.execute(
                    f"SELECT DISTINCT session_key FROM catalog_messages WHERE {ABSENT_MESSAGES} ORDER BY session_key"
                )
            ]
            export = archive_catalog_export_sessions(conn, session_keys)
            yield
    except BaseException:
        # An interrupt can land after COMMIT, so only rows still in the catalog prove the delete rolled back.
        try:
            rolled_back = export is not None and bool(archive_catalog_sessions_with_messages(conn, session_keys))
        except Exception:
            rolled_back = False
        if rolled_back:
            export.unlink(missing_ok=True)
        raise


def archive_catalog_prune(conn) -> None:
    with conn:
        archive_catalog_delete_messages(conn, SUPERSEDED_MESSAGES)
    with archive_catalog_absent_transaction(conn):
        archive_catalog_delete_messages(conn, ABSENT_MESSAGES)


def archive_catalog_upgrade_layout(conn) -> bool:
    row = conn.execute("SELECT value FROM catalog_meta WHERE key = 'layout_version'").fetchone()
    if row and int(row[0]) >= ARCHIVE_CATALOG_LAYOUT_VERSION:
        return True
    catalog = archive_catalog_db_path().expanduser()
    needed = catalog.stat().st_size + ARCHIVE_CATALOG_UPGRADE_HEADROOM_BYTES
    free = shutil.disk_usage(catalog.parent).free
    if free < needed:
        print(
            f"archive catalog cleanup skipped: {free // 2**20} MB free, needs {needed // 2**20} MB",
            file=sys.stderr,
        )
        return False
    secure_delete = conn.execute("PRAGMA secure_delete").fetchone()[0]
    # Zeroing the deleted rows would double the WAL on a full disk, and the closing VACUUM drops them anyway.
    conn.execute("PRAGMA secure_delete = OFF")
    try:
        with archive_catalog_absent_transaction(conn):
            conn.execute(f"DELETE FROM catalog_messages WHERE {ABSENT_MESSAGES}")
            conn.execute(f"DELETE FROM catalog_messages WHERE {SUPERSEDED_MESSAGES}")
            conn.execute("INSERT INTO catalog_message_fts (catalog_message_fts) VALUES ('rebuild')")
            for table in ARCHIVE_CATALOG_LEGACY_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.execute(
                """
                INSERT INTO catalog_meta (key, value) VALUES ('layout_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(ARCHIVE_CATALOG_LAYOUT_VERSION),),
            )
    finally:
        conn.execute(f"PRAGMA secure_delete = {SQLITE_SECURE_DELETE_MODES[secure_delete]}")
    return True


def report_cleanup_failure(step, conn, default):
    try:
        return step(conn)
    except (sqlite3.Error, OSError) as exc:
        print(f"archive catalog cleanup failed: {exc}", file=sys.stderr)
        return default


def archive_catalog_before_index(conn) -> bool:
    return report_cleanup_failure(archive_catalog_upgrade_layout, conn, False)


def archive_catalog_after_index(conn, layout_current: bool) -> None:
    if layout_current:
        report_cleanup_failure(archive_catalog_prune, conn, None)
