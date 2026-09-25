"""Read OpenCode session history for the archive catalog.

OpenCode keeps history in a SQLite database instead of per-session files
(since 1.2, which migrated older JSON storage on first run). Sessions are
listed from it read-only, top-level sessions only, like Codex subagents are
skipped, and their messages come from `opencode export`, the public format,
so only the small session listing depends on OpenCode's internal layout.

Failures raise OpenCodeError (an OSError), which the catalog treats like an
unreadable history file: it keeps what it already has and retries next run.
"""

import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

EXPORT_TIMEOUT_SECONDS = 30
MAX_EXPORT_BYTES = 64 * 1024 * 1024
DEFAULT_TITLE_RE = re.compile(r"^(New session|Child session) - \d{4}-\d{2}-\d{2}T")


class OpenCodeError(OSError):
    pass


class OpenCodeUnavailable(OpenCodeError):
    """The opencode binary cannot run at all; later exports would fail the same way."""


def default_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "opencode"


def opencode_binary() -> str:
    found = shutil.which("opencode")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".opencode" / "bin" / "opencode",
        home / ".local" / "bin" / "opencode",
        home / ".bun" / "bin" / "opencode",
        home / ".npm-global" / "bin" / "opencode",
        Path("/opt/homebrew/bin/opencode"),
        Path("/usr/local/bin/opencode"),
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return ""


def as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def session_row(session_id, title, directory, created_ms, updated_ms):
    title = title if isinstance(title, str) else ""
    created = as_int(created_ms)
    return {
        "session_id": str(session_id),
        "title": "" if DEFAULT_TITLE_RE.match(title) else title,
        "cwd": directory if isinstance(directory, str) else "",
        "created_ms": created,
        "updated_ms": as_int(updated_ms) or created,
    }


def database_sessions(database: Path):
    try:
        conn = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
            needed = {"id", "title", "directory", "parent_id", "time_created", "time_updated"}
            if not needed <= columns:
                raise OpenCodeError(f"unsupported OpenCode database layout in {database}")
            rows = conn.execute(
                "SELECT id, title, directory, time_created, time_updated FROM session WHERE parent_id IS NULL"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise OpenCodeError(f"cannot read OpenCode database {database}: {exc}") from exc
    return [session_row(*row) for row in rows]


def list_sessions(source_cfg):
    """Top-level OpenCode sessions; [] when OpenCode has no history here."""
    database = Path(source_cfg.get("database") or default_data_dir() / "opencode.db").expanduser()
    return database_sessions(database) if database.is_file() else []


def export_command(source_cfg, session_id):
    template = source_cfg.get("export") or ""
    if not template:
        binary = opencode_binary()
        if not binary:
            raise OpenCodeUnavailable("opencode is not installed or not on PATH")
        return [binary, "export", session_id]
    try:
        return [token.format(session_id=session_id) for token in shlex.split(template)]
    except (KeyError, IndexError, ValueError) as exc:
        raise OpenCodeUnavailable(f"invalid OpenCode export command {template!r}: {exc}") from exc


def export_messages(source_cfg, session_id):
    """Yield one item per user or assistant message with its plain text parts."""
    command = export_command(source_cfg, session_id)
    # OpenCode exits before a pipe drains and cuts exports short; a file gets all of it.
    with tempfile.TemporaryFile() as output:
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                stdout=output,
                stderr=subprocess.DEVNULL,
                timeout=EXPORT_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise OpenCodeError(f"opencode export {session_id} timed out") from exc
        except OSError as exc:
            raise OpenCodeUnavailable(f"cannot run opencode: {exc}") from exc
        size = output.seek(0, os.SEEK_END)
        if result.returncode != 0:
            raise OpenCodeError(f"opencode export {session_id} exited with {result.returncode}")
        if size > MAX_EXPORT_BYTES:
            raise OpenCodeError(f"opencode export {session_id} exceeds {MAX_EXPORT_BYTES} bytes")
        output.seek(0)
        raw = output.read()
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError as exc:
        raise OpenCodeError(f"opencode export {session_id} returned invalid JSON") from exc
    messages = data.get("messages") if isinstance(data, dict) else None
    if not isinstance(messages, list):
        raise OpenCodeError(f"opencode export {session_id} has no messages")
    for message in messages:
        if not isinstance(message, dict):
            continue
        info = message.get("info") if isinstance(message.get("info"), dict) else {}
        parts = message.get("parts") if isinstance(message.get("parts"), list) else []
        text = "\n".join(
            part["text"]
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and not part.get("synthetic")
            and isinstance(part.get("text"), str)
        )
        times = info.get("time") if isinstance(info.get("time"), dict) else {}
        yield {"role": info.get("role") or "", "text": text, "created_ms": as_int(times.get("created"))}
