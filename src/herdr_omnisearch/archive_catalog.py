import base64
import glob
import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

from .settings import app_config, cli_command, data_dir
from .storage import (
    archive_catalog_connect,
    connect,
    exclusive_lock,
    lock_is_held,
    spawn_locked_background,
    try_exclusive_lock,
)
from .textmatch import (
    clean_text,
    clip_text,
    fts_query,
    fuzzy_distance_limit,
    fuzzy_score_threshold,
    iso_to_epoch,
    parse_filters,
    prefix_upper_bound,
    score_token_candidate,
    shorten,
    title_from_text,
    tokens,
)
from . import opencode_history
from .live_index import (
    derive_space_label_from_cwd,
    fuzzy_snippet,
    live_space_label_for_cwd,
    live_space_label_for_session,
    live_space_labels_by_cwd,
    live_space_labels_by_session,
)

ARCHIVE_MAX_RECORD_BYTES = 2 * 1024 * 1024

# OpenCode sessions have no file per session; the catalog keys them by a
# virtual path and uses the session's update time as its change stamp.
OPENCODE_PREFIX = "opencode:"
OPENCODE_SOURCES = {}
# Caps one run's OpenCode exports so a hanging or missing opencode cannot hold
# the catalog lock (and every other agent's refresh) for long.
OPENCODE_RUN_SECONDS = 600
OPENCODE_MAX_TIMEOUTS = 2
OPENCODE_RUN = {}


def reset_opencode_run() -> None:
    OPENCODE_SOURCES.clear()
    OPENCODE_RUN.update(
        {"deadline": time.monotonic() + OPENCODE_RUN_SECONDS, "timeouts": 0, "stopped": ""}
    )


ARCHIVE_MESSAGE_MAX_CHARS = 16000


ARCHIVE_PREVIEW_MESSAGES = 3


ARCHIVE_CATALOG_CONTENT_VERSION = 6


ARCHIVE_PREFIX_MIN_CHARS = 4


def is_archive_noise(text: str) -> bool:
    text = clean_text(text)
    if not text:
        return True
    if text.startswith("{") and text.endswith("}"):
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            value = None
        approval_keys = {"outcome", "risk_level", "rationale", "user_authorization"}
        if (
            isinstance(value, dict)
            and "outcome" in value
            and set(value).issubset(approval_keys)
        ):
            return True
    noise_prefixes = (
        "# AGENTS.md instructions",
        "<environment_context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<apps_instructions>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "<INSTRUCTIONS>",
        "[Request interrupted by user",
        "<local-command-caveat>",
        "<local-command-stdout>",
        "<command-name>",
        "<command-message>",
        "[external_agent_tool_call",
        "[external_agent_tool_result",
        "<external_agent_tool",
        "<user_shell_command>",
        "The following is the Codex agent history",
        "The following skills are available",
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
    )
    if any(text.startswith(prefix) for prefix in noise_prefixes):
        return True
    if len(text) > 12000 and ("You are Codex" in text or "AGENTS.md" in text):
        return True
    return False


def is_archive_preview_noise(text: str) -> bool:
    value = " ".join(clean_text(text).lower().split())
    return value in {
        "continue",
        "continue?",
        "go on",
        "hello",
        "hi",
        "ok",
        "okay",
    }


def extract_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for item in content:
        if isinstance(item, str):
            pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("text", "input_text", "output_text"):
            pieces.append(item.get("text") or "")
    return "\n".join(piece for piece in pieces if piece)


def archive_window_bounds(window_days: int, window_offset: int = 0, *, now=None):
    """Return a stable local-calendar window as epoch seconds [start, end)."""
    window_days = max(1, int(window_days))
    window_offset = max(0, int(window_offset))
    current = datetime.fromtimestamp(time.time() if now is None else now)
    next_midnight = current.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    end = next_midnight - timedelta(days=window_days * window_offset)
    start = end - timedelta(days=window_days)
    return int(start.timestamp()), int(end.timestamp())


def archive_window_label(window_days: int, window_offset: int = 0, *, now=None) -> str:
    start, end = archive_window_bounds(window_days, window_offset, now=now)
    return archive_bounds_label(start, end)


def archive_bounds_label(start: int, end: int) -> str:
    first = datetime.fromtimestamp(start).strftime("%Y-%m-%d")
    last = datetime.fromtimestamp(end - 1).strftime("%Y-%m-%d")
    return f"{first} to {last}"


def load_codex_thread_names():
    path = Path(app_config()["archive"].get("codex", {}).get("thread_names", "")).expanduser()
    names = {}
    if not path or not path.exists():
        return names
    for item in iter_archive_records(path):
        session_id = item.get("id")
        if session_id:
            names[session_id] = item.get("thread_name") or ""
    return names


def iter_archive_records(path: Path, max_record_bytes: int = ARCHIVE_MAX_RECORD_BYTES):
    with path.open("rb") as handle:
        while True:
            record = handle.readline(max_record_bytes + 1)
            if not record:
                return
            complete = record.endswith(b"\n")
            oversized = len(record) > max_record_bytes
            while not complete:
                remainder = handle.readline(max_record_bytes + 1)
                if not remainder:
                    break
                complete = remainder.endswith(b"\n")
                oversized = True
            if oversized:
                continue
            try:
                item = json.loads(record.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                yield item


def is_opencode_source(agent: str) -> bool:
    return app_config()["archive"].get(agent, {}).get("kind") == "opencode"


def epoch_ms_iso(value) -> str:
    return datetime.fromtimestamp(int(value) / 1000).isoformat() if value else ""


def archive_paths(agent: str):
    if is_opencode_source(agent):
        sessions = opencode_history.list_sessions(app_config()["archive"][agent])
        paths = []
        for session in sessions:
            key = f"{OPENCODE_PREFIX}{agent}:{session['session_id']}"
            OPENCODE_SOURCES[key] = session
            paths.append(Path(key))
        return sorted(paths)
    globs = app_config()["archive"].get(agent, {}).get("sessions", [])
    paths = []
    for pattern in globs:
        paths.extend(Path(path) for path in glob.glob(os.path.expanduser(pattern), recursive=True))
    return sorted(paths)


def archive_source_stamp(agent: str, path: Path):
    """(size, mtime_ns) identifying one version of a source."""
    session = OPENCODE_SOURCES.get(str(path))
    if session is not None:
        return 0, session["updated_ms"] * 1_000_000
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns


def archive_source_items(agent: str, path: Path):
    session = OPENCODE_SOURCES.get(str(path))
    if session is None:
        yield from iter_archive_records(path)
        return
    if not OPENCODE_RUN:
        reset_opencode_run()
    if OPENCODE_RUN["stopped"] or time.monotonic() > OPENCODE_RUN["deadline"]:
        raise opencode_history.OpenCodeError(OPENCODE_RUN["stopped"] or "OpenCode export budget used up")
    try:
        messages = list(opencode_history.export_messages(app_config()["archive"][agent], session["session_id"]))
    except opencode_history.OpenCodeUnavailable as exc:
        OPENCODE_RUN["stopped"] = str(exc)
        raise
    except opencode_history.OpenCodeError as exc:
        if "timed out" in str(exc):
            OPENCODE_RUN["timeouts"] += 1
            if OPENCODE_RUN["timeouts"] >= OPENCODE_MAX_TIMEOUTS:
                OPENCODE_RUN["stopped"] = "opencode export keeps timing out"
        raise
    OPENCODE_RUN["timeouts"] = 0
    for message in messages:
        yield {**message, "timestamp": epoch_ms_iso(message["created_ms"])}


def archive_path_timestamp(agent: str, path: Path) -> float:
    session = OPENCODE_SOURCES.get(str(path))
    if session is not None:
        return session["created_ms"] / 1000
    match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})", path.name)
    if match:
        try:
            return datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S").timestamp()
        except ValueError:
            pass
    try:
        for item in iter_archive_records(path):
            timestamp = item.get("timestamp") or ""
            if agent == "codex" and item.get("type") == "session_meta":
                timestamp = (item.get("payload") or {}).get("timestamp") or timestamp
            parsed = iso_to_epoch(timestamp)
            if parsed:
                return parsed
    except OSError:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def codex_session_is_subagent(payload) -> bool:
    source = payload.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return True
    return bool(payload.get("parent_thread_id"))


def opencode_metadata(agent: str, path: Path):
    session = OPENCODE_SOURCES[str(path)]
    return {
        "agent": agent,
        "session_id": session["session_id"],
        "title": session["title"],
        "cwd": session["cwd"],
        "path": str(path),
        "started_at": epoch_ms_iso(session["created_ms"]),
        "updated_at": epoch_ms_iso(session["updated_ms"]),
        "is_subagent": False,
    }


def archive_file_metadata(agent: str, path: Path, thread_names):
    if str(path) in OPENCODE_SOURCES:
        return opencode_metadata(agent, path)
    session_id = path.stem
    title = ""
    cwd = ""
    slug = ""
    is_subagent = False
    try:
        for item in iter_archive_records(path):
            if agent == "codex":
                item_type = item.get("type")
                payload = item.get("payload") or {}
                if item_type == "session_meta":
                    session_id = payload.get("id") or session_id
                    cwd = payload.get("cwd") or cwd
                    is_subagent = codex_session_is_subagent(payload)
                    title = thread_names.get(session_id) or title
                    if title:
                        break
                    continue
                if item_type == "turn_context":
                    cwd = payload.get("cwd") or cwd
                    continue
                if (
                    item_type == "response_item"
                    and payload.get("type") == "message"
                    and payload.get("role") == "user"
                ):
                    candidate = extract_message_text(payload.get("content"))
                    if not is_archive_noise(candidate):
                        title = title_from_text(candidate, "")
                        if title:
                            break
            elif agent == "claude":
                session_id = item.get("sessionId") or session_id
                cwd = item.get("cwd") or cwd
                slug = item.get("slug") or slug
                if session_id and (cwd or slug):
                    break
            else:
                return None
    except OSError:
        return None
    if agent == "codex":
        title = title or thread_names.get(session_id) or ""
    elif slug:
        title = slug.replace("-", " ")
    started_epoch = archive_path_timestamp(agent, path)
    started_at = datetime.fromtimestamp(started_epoch).isoformat() if started_epoch else ""
    return {
        "agent": agent,
        "session_id": session_id,
        "title": title,
        "cwd": cwd,
        "path": str(path),
        "started_at": started_at,
        "updated_at": started_at,
        "is_subagent": is_subagent,
    }


def selected_archive_sources(agents: str):
    configured = app_config()["archive_agents"]
    selected = {agent.strip() for agent in (agents or ",".join(configured)).split(",") if agent.strip()}
    return selected or set(configured)


def archive_catalog_turn(agent: str, item):
    role = ""
    content = None
    if agent == "codex":
        payload = item.get("payload") or {}
        if item.get("type") == "response_item" and payload.get("type") == "message":
            role = payload.get("role") or ""
            content = payload.get("content")
    elif agent == "claude" and item.get("type") in ("user", "assistant"):
        message = item.get("message") or {}
        role = message.get("role") or item.get("type") or ""
        content = message.get("content")
    elif is_opencode_source(agent):
        role = item.get("role") or ""
        content = item.get("text") or ""
    if role not in ("user", "assistant"):
        return None
    text = extract_message_text(content)
    if is_archive_noise(text):
        return None
    text = clip_text(text, ARCHIVE_MESSAGE_MAX_CHARS)
    if not text:
        return None
    timestamp = item.get("timestamp") or ""
    return {
        "role": role,
        "message_at": timestamp,
        "message_epoch": int(iso_to_epoch(timestamp) or 0),
        "content": text,
    }


def archive_catalog_preview(turns) -> str:
    return "\n".join(
        f"{turn['role']}: {shorten(turn['content'], 900)}"
        for turn in turns
    )


def archive_catalog_is_wrapper(title: str, preview: str) -> bool:
    value = f"{title}\n{preview}".lower()
    markers = (
        "agent history whose request action you are assessing",
        ">>> approval request start",
        "assess the exact planned action below",
    )
    return any(marker in value for marker in markers)


def archive_catalog_document(
    agent: str,
    path: Path,
    thread_names,
    message_sink=None,
    metadata=None,
):
    metadata = metadata or archive_file_metadata(agent, path, thread_names)
    if not metadata:
        return None
    recent_turns = []
    first_turns = []
    message_count = 0
    updated_at = metadata.get("updated_at") or metadata.get("started_at") or ""
    try:
        for item in archive_source_items(agent, path):
            timestamp = item.get("timestamp") or ""
            if timestamp:
                updated_at = timestamp
            turn = archive_catalog_turn(agent, item)
            if not turn:
                continue
            turn["ordinal"] = message_count
            if message_sink is not None:
                message_sink(turn)
            if len(first_turns) < ARCHIVE_PREVIEW_MESSAGES:
                first_turns.append(turn)
            if not is_archive_preview_noise(turn["content"]):
                recent_turns.append(turn)
                if len(recent_turns) > ARCHIVE_PREVIEW_MESSAGES:
                    recent_turns.pop(0)
            message_count += 1
    except OSError:
        return None
    metadata["updated_at"] = updated_at
    if not metadata.get("title") and str(path) in OPENCODE_SOURCES:
        # Untitled OpenCode sessions carry a timestamp title; use the first request instead.
        first_user = next((turn for turn in first_turns if turn["role"] == "user"), None)
        metadata["title"] = title_from_text(first_user["content"], "") if first_user else ""
    metadata["preview"] = archive_catalog_preview(recent_turns)
    metadata["message_count"] = message_count
    metadata["space_label"] = derive_space_label_from_cwd(metadata.get("cwd") or "")
    metadata["started_epoch"] = int(
        iso_to_epoch(metadata.get("started_at") or "") or archive_path_timestamp(agent, path)
    )
    metadata["updated_epoch"] = int(
        iso_to_epoch(metadata.get("updated_at") or "") or metadata["started_epoch"]
    )
    metadata["is_wrapper"] = int(
        bool(metadata.pop("is_subagent", False))
        or archive_catalog_is_wrapper(
            metadata.get("title") or "",
            archive_catalog_preview(first_turns),
        )
    )
    return metadata, message_count


def archive_catalog_store_message_batch(conn, session_key: str, generation: int, turns) -> None:
    if not turns:
        return
    rows = []
    for turn in turns:
        material = "\0".join(
            (
                session_key,
                str(turn["ordinal"]),
                turn["role"],
                turn["content"],
            )
        )
        rows.append(
            {
                **turn,
                "message_key": hashlib.sha1(
                    material.encode("utf-8", "replace")
                ).hexdigest(),
                "session_key": session_key,
                "indexed_generation": generation,
            }
        )

    keys = [row["message_key"] for row in rows]
    placeholders = ",".join("?" for _ in keys)
    existing = {
        row["message_key"]
        for row in conn.execute(
            f"SELECT message_key FROM catalog_messages WHERE message_key IN ({placeholders})",
            keys,
        ).fetchall()
    }
    conn.executemany(
        """
        INSERT INTO catalog_messages (
            message_key, session_key, ordinal, role, message_at, message_epoch,
            content, indexed_generation
        ) VALUES (
            :message_key, :session_key, :ordinal, :role, :message_at, :message_epoch,
            :content, :indexed_generation
        )
        ON CONFLICT(message_key) DO UPDATE SET
            session_key = excluded.session_key,
            ordinal = excluded.ordinal,
            role = excluded.role,
            message_at = excluded.message_at,
            message_epoch = excluded.message_epoch,
            indexed_generation = excluded.indexed_generation
        """,
        rows,
    )
    new_rows = [row for row in rows if row["message_key"] not in existing]
    if not new_rows:
        return
    ids = {
        row["message_key"]: row["id"]
        for row in conn.execute(
            f"SELECT id, message_key FROM catalog_messages WHERE message_key IN ({placeholders})",
            keys,
        ).fetchall()
    }
    conn.executemany(
        "INSERT INTO catalog_message_fts (rowid, content) VALUES (?, ?)",
        ((ids[row["message_key"]], row["content"]) for row in new_rows),
    )


def archive_catalog_index(agents: str = ""):
    require_archive_enabled()
    inherited_lock = os.environ.get("HERDR_OMNISEARCH_CATALOG_LOCK_FD")
    lock_fd = None
    if not inherited_lock:
        lock_fd = exclusive_lock(data_dir() / "archive-catalog.lock")
    try:
        return _archive_catalog_index_unlocked(agents)
    finally:
        if lock_fd is not None:
            os.close(lock_fd)


def _archive_catalog_index_unlocked(agents: str = ""):
    selected = selected_archive_sources(agents)
    thread_names = load_codex_thread_names() if "codex" in selected else {}
    reset_opencode_run()
    sources = []
    # Agents whose history cannot be listed this run keep their catalog rows as they are.
    unlisted = set()
    for agent in sorted(selected):
        try:
            sources.extend((agent, path) for path in archive_paths(agent))
        except opencode_history.OpenCodeError:
            unlisted.add(agent)
    sources = list(dict.fromkeys(sources))
    sources.sort(
        key=lambda item: archive_path_timestamp(item[0], item[1]),
        reverse=True,
    )
    live_session_spaces = {}
    live_spaces = {}
    live_conn = None
    try:
        live_conn = connect()
        live_session_spaces = live_space_labels_by_session(live_conn)
        live_spaces = live_space_labels_by_cwd(live_conn)
    except (sqlite3.Error, OSError):
        pass
    finally:
        if live_conn is not None:
            live_conn.close()

    conn = archive_catalog_connect()
    existing = {
        row["path"]: dict(row)
        for row in conn.execute(
            """
            SELECT session_key, agent, session_id, space_label, space_label_source, cwd,
                   path, source_size, source_mtime_ns, started_epoch,
                   message_index_version, is_present
            FROM catalog_sessions
            """
        ).fetchall()
    }
    source_paths = {str(path) for _agent, path in sources}
    changed = 0
    unchanged = 0
    removed = 0
    message_count = 0
    indexed_at = int(time.time())
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO catalog_meta (key, value) VALUES ('index_started_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(indexed_at),),
            )
            stale_paths = {
                path
                for path, row in existing.items()
                if row["agent"] in selected and row["agent"] not in unlisted and path not in source_paths
            }
            for stale_path in sorted(stale_paths):
                old = existing[stale_path]
                if int(old["is_present"] or 0):
                    conn.execute(
                        "UPDATE catalog_sessions SET is_present = 0 WHERE session_key = ?",
                        (old["session_key"],),
                    )
                    removed += 1

            for old in existing.values():
                mapped_label = (
                    live_session_spaces.get((old["agent"], old["session_id"]))
                    or live_spaces.get(clean_text(old.get("cwd") or ""))
                )
                if mapped_label and (
                    mapped_label != old.get("space_label")
                    or old.get("space_label_source") != "herdr"
                ):
                    conn.execute(
                        """
                        UPDATE catalog_sessions
                        SET space_label = ?, space_label_source = 'herdr'
                        WHERE session_key = ?
                        """,
                        (mapped_label, old["session_key"]),
                    )
                    old["space_label"] = mapped_label
                    old["space_label_source"] = "herdr"

        for agent, path in sources:
            try:
                source_size, source_mtime_ns = archive_source_stamp(agent, path)
            except OSError:
                continue
            old = existing.get(str(path))
            if (
                old
                and int(old["source_size"]) == source_size
                and int(old["source_mtime_ns"]) == source_mtime_ns
                and int(old["started_epoch"] or 0) > 0
                and int(old["message_index_version"] or 0)
                == ARCHIVE_CATALOG_CONTENT_VERSION
                and int(old["is_present"] or 0) == 1
            ):
                unchanged += 1
                continue
            base_metadata = archive_file_metadata(agent, path, thread_names)
            if not base_metadata:
                continue
            session_key = old["session_key"] if old else f"{agent}:{base_metadata['session_id']}"
            generation = time.time_ns()
            message_batch = []

            def store_turn(turn):
                message_batch.append(turn)
                if len(message_batch) >= 200:
                    archive_catalog_store_message_batch(
                        conn,
                        session_key,
                        generation,
                        message_batch,
                    )
                    message_batch.clear()

            conn.execute("SAVEPOINT archive_catalog_source")
            document = archive_catalog_document(
                agent,
                path,
                thread_names,
                message_sink=store_turn,
                metadata=base_metadata,
            )
            if not document:
                conn.execute("ROLLBACK TO SAVEPOINT archive_catalog_source")
                conn.execute("RELEASE SAVEPOINT archive_catalog_source")
                continue
            archive_catalog_store_message_batch(
                conn,
                session_key,
                generation,
                message_batch,
            )
            metadata, indexed_messages = document
            mapped_label = (
                live_session_spaces.get((agent, metadata["session_id"]))
                or live_spaces.get(clean_text(metadata.get("cwd") or ""))
            )
            old_label = old.get("space_label") if old else ""
            metadata["space_label"] = (
                mapped_label
                or (old_label if old and old.get("space_label_source") == "herdr" else "")
                or metadata["space_label"]
            )
            metadata["space_label_source"] = (
                "herdr"
                if mapped_label or (old and old.get("space_label_source") == "herdr")
                else "derived"
            )
            row = {
                **metadata,
                "session_key": session_key,
                "source_size": source_size,
                "source_mtime_ns": source_mtime_ns,
                "indexed_at": indexed_at,
                "message_generation": generation,
                "message_index_version": ARCHIVE_CATALOG_CONTENT_VERSION,
                "is_present": 1,
            }
            conn.execute(
                """
                INSERT INTO catalog_sessions (
                    session_key, agent, session_id, space_label, space_label_source, title, cwd, path,
                    started_at, updated_at, started_epoch, updated_epoch, preview, is_wrapper,
                    source_size, source_mtime_ns, indexed_at, message_generation,
                    message_count, message_index_version, is_present
                ) VALUES (
                    :session_key, :agent, :session_id, :space_label, :space_label_source, :title, :cwd, :path,
                    :started_at, :updated_at, :started_epoch, :updated_epoch, :preview, :is_wrapper,
                    :source_size, :source_mtime_ns, :indexed_at, :message_generation,
                    :message_count, :message_index_version, :is_present
                )
                ON CONFLICT(session_key) DO UPDATE SET
                    agent = excluded.agent,
                    session_id = excluded.session_id,
                    space_label = excluded.space_label,
                    space_label_source = excluded.space_label_source,
                    title = excluded.title,
                    cwd = excluded.cwd,
                    path = excluded.path,
                    started_at = excluded.started_at,
                    updated_at = excluded.updated_at,
                    started_epoch = excluded.started_epoch,
                    updated_epoch = excluded.updated_epoch,
                    preview = excluded.preview,
                    is_wrapper = excluded.is_wrapper,
                    source_size = excluded.source_size,
                    source_mtime_ns = excluded.source_mtime_ns,
                    indexed_at = excluded.indexed_at,
                    message_generation = excluded.message_generation,
                    message_count = excluded.message_count,
                    message_index_version = excluded.message_index_version,
                    is_present = excluded.is_present
                """,
                row,
            )
            conn.execute("RELEASE SAVEPOINT archive_catalog_source")
            conn.commit()
            changed += 1
            message_count += indexed_messages

        with conn:
            conn.execute(
                """
                INSERT INTO catalog_meta (key, value) VALUES ('last_indexed_at', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(int(time.time())),),
            )
    finally:
        conn.close()
    return changed, unchanged, removed, message_count


def archive_catalog_state():
    conn = archive_catalog_connect()
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM catalog_sessions WHERE COALESCE(is_present, 1) = 1"
        ).fetchone()[0]
        last = conn.execute(
            "SELECT value FROM catalog_meta WHERE key = 'last_indexed_at'"
        ).fetchone()
    finally:
        conn.close()
    return count, int(last[0]) if last else 0


def archive_catalog_state_name(count: int, last: int) -> str:
    if lock_is_held(data_dir() / "archive-catalog.lock"):
        return "updating"
    if not count:
        return "empty"
    if not last:
        return "partial"
    return "ready"


def maybe_background_archive_catalog_index(agents: str = "", stale_seconds: int = 60):
    require_archive_enabled()
    count, last = archive_catalog_state()
    if count and int(time.time()) - last < stale_seconds:
        return
    lock_path = data_dir() / "archive-catalog.lock"
    lock_fd = try_exclusive_lock(lock_path)
    if lock_fd is None:
        return
    cmd = [*cli_command(), "archive-catalog-index", "--agents", agents]
    spawn_locked_background(
        cmd,
        lock_fd,
        lock_env="HERDR_OMNISEARCH_CATALOG_LOCK_FD",
    )


def archive_catalog_candidate_tokens(conn, term: str, limit: int = 24, exclude=None):
    term = term.lower()
    exclude = {token.lower() for token in (exclude or ())}
    if len(term) < 4:
        return []
    distance = fuzzy_distance_limit(term)
    candidates = set()
    prefixes = [term[:2]]
    if len(term) >= 8:
        prefixes.append(term[:1])
    for prefix in prefixes:
        upper = prefix_upper_bound(prefix)
        if not upper:
            continue
        rows = conn.execute(
            """
            SELECT term
            FROM catalog_message_vocab
            WHERE term >= ? AND term < ?
              AND length(term) BETWEEN ? AND ?
            ORDER BY abs(length(term) - ?) ASC, term ASC
            LIMIT 12000
            """,
            (
                prefix,
                upper,
                max(2, len(term) - distance),
                len(term) + distance,
                len(term),
            ),
        ).fetchall()
        candidates.update(row["term"] for row in rows)
        scored = [
            (candidate, score_token_candidate(term, candidate))
            for candidate in candidates
            if candidate not in exclude
        ]
        scored = [item for item in scored if item[1] >= fuzzy_score_threshold(term)]
        if scored:
            scored.sort(
                key=lambda item: (
                    -item[1],
                    abs(len(item[0]) - len(term)),
                    item[0],
                )
            )
            best_score = scored[0][1]
            return [item for item in scored if item[1] >= best_score - 0.02][:limit]
    return []


def archive_catalog_fuzzy_query(conn, query_terms, *, exclude_exact=False):
    groups = []
    for term in query_terms:
        candidates = archive_catalog_candidate_tokens(
            conn,
            term,
            limit=24,
            exclude={term} if exclude_exact else None,
        )
        if not candidates:
            return ""
        quoted = []
        for token, _score in candidates:
            quoted.append('"' + token.replace('"', '""') + '"')
        groups.append("(" + " OR ".join(quoted) + ")")
    return " ".join(groups)


def archive_catalog_matched_tokens(content: str, query_terms):
    content_terms = set(tokens(content or ""))
    matched = []
    for query_term in query_terms:
        candidates = [
            (score_token_candidate(query_term, candidate), candidate)
            for candidate in content_terms
        ]
        candidates = [candidate for candidate in candidates if candidate[0] > 0]
        if candidates:
            matched.append(max(candidates)[1])
    return list(dict.fromkeys(matched))


def archive_catalog_context(conn, row, matched_tokens) -> str:
    ordinal = row.get("match_ordinal")
    if ordinal is None:
        return row.get("preview") or row.get("title") or ""
    turns = conn.execute(
        """
        SELECT ordinal, role, content
        FROM catalog_messages
        WHERE session_key = ?
          AND indexed_generation = ?
          AND ordinal BETWEEN ? AND ?
        ORDER BY ordinal ASC
        """,
        (
            row["session_key"],
            row["message_generation"],
            max(0, int(ordinal) - 1),
            int(ordinal) + 1,
        ),
    ).fetchall()
    lines = []
    for turn in turns:
        if int(turn["ordinal"]) == int(ordinal):
            text = fuzzy_snippet(turn["content"], matched_tokens)
        else:
            text = shorten(turn["content"], 420)
        lines.append(f"{turn['role']}: {text}")
    return "\n".join(lines) or row.get("match_content") or row.get("preview") or ""


def archive_catalog_result_row(row, conn=None, query_terms=None):
    result = dict(row)
    matched_tokens = archive_catalog_matched_tokens(
        result.get("match_content") or "",
        query_terms or [],
    )
    if conn is not None and result.get("match_ordinal") is not None:
        content = archive_catalog_context(conn, result, matched_tokens)
    else:
        content = result.get("preview") or result.get("title") or ""
    result.update(
        {
            "stable_id": archive_catalog_stable_id(result["session_key"]),
            "content": content,
            "snippet": content,
            "matched_tokens": matched_tokens,
            "match_count": int(result.get("match_count") or 1),
            "source": "archive",
            "agent_status": "archive",
            "workspace_label": result.get("space_label") or "archive",
            "pane_label": result.get("title") or result.get("session_id"),
            "pane_id": result.get("session_id"),
            "foreground_cwd": result.get("cwd") or "",
            "_archive_catalog": True,
        }
    )
    return result


def archive_catalog_stable_id(session_key: str) -> str:
    encoded = base64.urlsafe_b64encode(session_key.encode("utf-8")).decode("ascii")
    return "archive-catalog:" + encoded.rstrip("=")


def archive_catalog_session_key(stable_id: str) -> str:
    prefix = "archive-catalog:"
    if not stable_id.startswith(prefix):
        return ""
    encoded = stable_id[len(prefix) :]
    try:
        padding = "=" * (-len(encoded) % 4)
        return base64.urlsafe_b64decode(encoded + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def archive_catalog_result(stable_id: str):
    session_key = archive_catalog_session_key(stable_id)
    if not session_key:
        return None
    conn = archive_catalog_connect()
    try:
        row = conn.execute(
            """
            SELECT *, 0.0 AS rank
            FROM catalog_sessions
            WHERE session_key = ? AND COALESCE(is_present, 1) = 1
            """,
            (session_key,),
        ).fetchone()
    finally:
        conn.close()
    return archive_catalog_result_row(row) if row else None


def archive_catalog_max_window_offset(agent, window_days: int, *, now=None) -> int:
    conn = archive_catalog_connect()
    try:
        if agent:
            row = conn.execute(
                """
                SELECT MIN(started_epoch)
                FROM catalog_sessions
                WHERE agent = ? AND COALESCE(is_present, 1) = 1
                """,
                (agent,),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT MIN(started_epoch)
                FROM catalog_sessions
                WHERE COALESCE(is_present, 1) = 1
                """
            ).fetchone()
    finally:
        conn.close()
    oldest = int(row[0] or 0) if row else 0
    if not oldest:
        return 0
    offset = 0
    while True:
        start, _end = archive_window_bounds(window_days, offset, now=now)
        if oldest >= start:
            return offset
        offset += 1


def archive_catalog_fts_query(query: str) -> str:
    return fts_query(query, prefix_min_chars=ARCHIVE_PREFIX_MIN_CHARS)


def archive_catalog_workspace_matches(conn, clauses, params, query_terms, limit):
    if not query_terms:
        return []
    rows = conn.execute(
        f"""
        SELECT s.session_key, s.space_label, s.updated_epoch, s.title
        FROM catalog_sessions s
        WHERE {' AND '.join(clauses)}
          AND s.space_label_source = 'herdr'
        """,
        params,
    ).fetchall()
    query_label = " ".join(query_terms)
    matches = []
    for row in rows:
        label = row["space_label"] or ""
        label_terms = list(dict.fromkeys(tokens(label)))
        matched_tokens = []
        quality = 0.0
        for query_term in query_terms:
            candidates = [
                (score_token_candidate(query_term, label_term), label_term)
                for label_term in label_terms
            ]
            if not candidates:
                break
            score, matched_token = max(candidates)
            if score < fuzzy_score_threshold(query_term):
                break
            quality += score
            matched_tokens.append(matched_token)
        else:
            normalized_label = " ".join(label_terms)
            if normalized_label == query_label:
                quality += 2.0
            elif query_label in normalized_label:
                quality += 1.0
            matches.append(
                {
                    "session_key": row["session_key"],
                    "matched_tokens": list(dict.fromkeys(matched_tokens)),
                    "_space_match_score": len(query_terms),
                    "_workspace_match_quality": quality,
                    "updated_epoch": row["updated_epoch"],
                    "title": row["title"],
                }
            )
    matches.sort(
        key=lambda row: (
            -float(row.get("_workspace_match_quality") or 0.0),
            -int(row.get("updated_epoch") or 0),
            row.get("title") or "",
        )
    )
    matches = matches[:limit]
    hydrated = []
    for match in matches:
        row = conn.execute(
            "SELECT *, 0.0 AS rank FROM catalog_sessions WHERE session_key = ?",
            (match["session_key"],),
        ).fetchone()
        result = archive_catalog_result_row(row)
        result.update(
            {
                "matched_tokens": match["matched_tokens"],
                "_space_match_score": match["_space_match_score"],
                "_workspace_match_quality": match["_workspace_match_quality"],
            }
        )
        hydrated.append(result)
    return hydrated


def archive_catalog_search(
    query: str,
    limit: int,
    *,
    agent=None,
    window_days=None,
    window_offset=None,
):
    text, filters = parse_filters(query)
    if agent:
        filters["agent"] = agent
    clauses = ["COALESCE(s.is_wrapper, 0) = 0", "COALESCE(s.is_present, 1) = 1"]
    params = {"limit": limit}
    if filters.get("agent"):
        clauses.append("s.agent = :agent")
        params["agent"] = filters["agent"]
    if filters.get("cwd"):
        clauses.append("COALESCE(s.cwd, '') LIKE :cwd")
        params["cwd"] = f"%{filters['cwd']}%"
    if filters.get("workspace"):
        clauses.append(
            "(COALESCE(s.space_label, '') LIKE :workspace OR COALESCE(s.title, '') LIKE :workspace OR COALESCE(s.cwd, '') LIKE :workspace)"
        )
        params["workspace"] = f"%{filters['workspace']}%"
    if window_days is not None and window_offset is not None:
        start, end = archive_window_bounds(window_days, window_offset)
        clauses.extend(["s.started_epoch >= :window_start", "s.started_epoch < :window_end"])
        params["window_start"] = start
        params["window_end"] = end

    conn = archive_catalog_connect()
    query_terms = list(dict.fromkeys(tokens(text)))
    rows = []
    try:
        if query_terms:
            workspace_rows = archive_catalog_workspace_matches(
                conn,
                clauses,
                params,
                query_terms,
                limit,
            )
            exact = archive_catalog_fts_query(text)
            candidate_limit = min(400, max(limit * 10, 100))

            def run_message_search(search):
                sql = f"""
                    SELECT s.*, m.ordinal AS match_ordinal, m.role AS match_role,
                           m.message_at AS match_at, m.content AS match_content,
                           bm25(catalog_message_fts) AS rank
                    FROM catalog_message_fts
                    JOIN catalog_messages m ON m.id = catalog_message_fts.rowid
                    JOIN catalog_sessions s ON s.session_key = m.session_key
                    WHERE catalog_message_fts MATCH :search
                      AND m.indexed_generation = s.message_generation
                      AND {' AND '.join(clauses)}
                    ORDER BY rank ASC, m.message_epoch DESC, s.updated_epoch DESC
                    LIMIT :candidate_limit
                """
                return conn.execute(
                    sql,
                    {**params, "search": search, "candidate_limit": candidate_limit},
                ).fetchall()

            matched = run_message_search(exact) if exact else []
            if not matched:
                fuzzy = archive_catalog_fuzzy_query(
                    conn,
                    query_terms,
                    exclude_exact=True,
                )
                if fuzzy and fuzzy != exact:
                    matched = run_message_search(fuzzy)
            grouped = {}
            for row in matched:
                session_key = row["session_key"]
                current = grouped.get(session_key)
                if current is None:
                    current = dict(row)
                    current["match_count"] = 1
                    grouped[session_key] = current
                else:
                    current["match_count"] += 1
            ranked = list(grouped.values())
            ranked.sort(
                key=lambda row: (
                    float(row.get("rank") or 0.0),
                    -int(row.get("updated_epoch") or 0),
                )
            )
            message_rows = [
                archive_catalog_result_row(row, conn, query_terms)
                for row in ranked[:limit]
            ]
            rows = list(workspace_rows)
            seen_sessions = {row["session_key"] for row in rows}
            rows.extend(
                row
                for row in message_rows
                if row["session_key"] not in seen_sessions
            )
            rows = rows[:limit]
        else:
            clauses.append("COALESCE(s.preview, '') <> ''")
            sql = f"""
                SELECT s.*, 0.0 AS rank
                FROM catalog_sessions s
                WHERE {' AND '.join(clauses)}
                ORDER BY s.started_epoch DESC, s.title ASC
                LIMIT :limit
            """
            rows = [
                archive_catalog_result_row(row)
                for row in conn.execute(sql, params).fetchall()
            ]
    finally:
        conn.close()
    return rows[:limit]


def require_archive_enabled() -> None:
    if not app_config()["archive_enabled"]:
        raise RuntimeError(
            "archive indexing is disabled; set enabled = true in the [archive] config section"
        )


def mark_archive_row(row, conn=None, space_cache=None):
    row["source"] = "archive"
    row["agent_status"] = "archive"
    row["workspace_label"] = archive_space_label(row, conn=conn, cache=space_cache)
    row["pane_label"] = row.get("title") or row.get("session_id")
    row["pane_id"] = row.get("session_id")
    row["foreground_cwd"] = row.get("cwd") or ""
    return row


def is_archive_placeholder_label(label: str) -> bool:
    label = " ".join(clean_text(label or "").split()).lower()
    return label == "archive" or label.startswith("archive ")


def archive_space_label(row, conn=None, cache=None) -> str:
    session_cache = None
    cwd_cache = cache
    if cache is not None:
        session_cache = cache.setdefault("__session__", {})
        cwd_cache = cache.setdefault("__cwd__", {})
    live_session_label = live_space_label_for_session(
        row.get("agent") or "",
        row.get("session_id") or "",
        conn=conn,
        cache=session_cache,
    )
    if live_session_label and not is_archive_placeholder_label(live_session_label):
        return live_session_label
    live_label = live_space_label_for_cwd(row.get("cwd") or "", conn=conn, cache=cwd_cache)
    if live_label and not is_archive_placeholder_label(live_label):
        return live_label
    if row.get("space_label") and not is_archive_placeholder_label(row["space_label"]):
        return row["space_label"]
    return derive_space_label_from_cwd(row.get("cwd") or "")
