import os
import re
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path

from .settings import app_config
from .textmatch import clean_text, clip_text, iso_to_epoch, title_from_text
from . import opencode_history

ARCHIVE_MAX_RECORD_BYTES = 2 * 1024 * 1024
ARCHIVE_MESSAGE_MAX_CHARS = 16000

# OpenCode has no file per session, so the catalog keys each one by a virtual path.
OPENCODE_PREFIX = "opencode:"
OPENCODE_SOURCES = {}
# A hanging opencode must not hold the catalog lock, and every other agent's refresh, for long.
OPENCODE_RUN_SECONDS = 600
OPENCODE_MAX_TIMEOUTS = 2
OPENCODE_RUN = {}


def reset_opencode_run() -> None:
    OPENCODE_SOURCES.clear()
    OPENCODE_RUN.update(
        {"deadline": time.monotonic() + OPENCODE_RUN_SECONDS, "timeouts": 0, "stopped": ""}
    )


def is_archive_noise(text: str) -> bool:
    import json
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


def load_codex_thread_names(agent: str):
    value = app_config()["archive"].get(agent, {}).get("thread_names")
    path = Path(value).expanduser() if value else None
    names = {}
    if path is None or not path.exists():
        return names
    for item in iter_archive_records(path):
        session_id = item.get("id")
        if session_id:
            names[session_id] = item.get("thread_name") or ""
    return names


def iter_archive_records(path: Path, max_record_bytes: int = ARCHIVE_MAX_RECORD_BYTES):
    import json
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
    import glob
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


def item_time(item) -> str:
    return item.get("timestamp") or ""


def user_message_title(turn) -> str:
    if not turn or turn[0] != "user":
        return ""
    text = extract_message_text(turn[1])
    return "" if is_archive_noise(text) else title_from_text(text, "")


def codex_turn(item):
    payload = item.get("payload") or {}
    if item.get("type") == "response_item" and payload.get("type") == "message":
        return payload.get("role") or "", payload.get("content")
    return None


def codex_time(item) -> str:
    if item.get("type") == "session_meta":
        return (item.get("payload") or {}).get("timestamp") or item_time(item)
    return item_time(item)


def codex_session_is_subagent(payload) -> bool:
    source = payload.get("source")
    if isinstance(source, dict) and "subagent" in source:
        return True
    return bool(payload.get("parent_thread_id"))


def codex_metadata(path: Path, thread_names):
    session_id, title, cwd, is_subagent = path.stem, "", "", False
    for item in iter_archive_records(path):
        item_type = item.get("type")
        payload = item.get("payload") or {}
        if item_type == "session_meta":
            session_id = payload.get("id") or session_id
            cwd = payload.get("cwd") or cwd
            is_subagent = codex_session_is_subagent(payload)
            title = thread_names.get(session_id) or title
            if title:
                break
        elif item_type == "turn_context":
            cwd = payload.get("cwd") or cwd
        else:
            title = user_message_title(codex_turn(item))
            if title:
                break
    title = title or thread_names.get(session_id) or ""
    return {"session_id": session_id, "title": title, "cwd": cwd, "is_subagent": is_subagent}


def claude_turn(item):
    if item.get("type") in ("user", "assistant"):
        message = item.get("message") or {}
        return message.get("role") or item.get("type") or "", message.get("content")
    return None


def claude_metadata(path: Path, _thread_names):
    session_id, cwd, slug = path.stem, "", ""
    for item in iter_archive_records(path):
        session_id = item.get("sessionId") or session_id
        cwd = item.get("cwd") or cwd
        slug = item.get("slug") or slug
        if session_id and (cwd or slug):
            break
    return {"session_id": session_id, "title": slug.replace("-", " "), "cwd": cwd, "is_subagent": False}


def omp_turn(item):
    if item.get("type") == "message":
        message = item.get("message") or {}
        return message.get("role") or "", message.get("content")
    return None


def omp_metadata(path: Path, _thread_names):
    session_id, title, cwd = "", "", ""
    # omp writes a subagent's transcript into the folder named after its parent session's file.
    is_subagent = path.name.startswith("__") or Path(f"{path.parent}.jsonl").is_file()
    for item in iter_archive_records(path):
        item_type = item.get("type")
        if item_type == "session":
            session_id = item.get("id") or session_id
            cwd = item.get("cwd") or cwd
            title = item.get("title") or title
            if title and cwd:
                break
        elif item_type == "title":
            title = item.get("title") or title
        elif not title:
            title = user_message_title(omp_turn(item))
    session_id = session_id or path.stem.rsplit("_", 1)[-1]
    return {"session_id": session_id, "title": title, "cwd": cwd, "is_subagent": is_subagent}


def opencode_turn(item):
    return item.get("role") or "", item.get("text") or ""


def opencode_metadata(path: Path, _thread_names):
    session = OPENCODE_SOURCES.get(str(path))
    if session is None:
        return None
    return {
        "session_id": session["session_id"],
        "title": session["title"],
        "cwd": session["cwd"],
        "started_at": epoch_ms_iso(session["created_ms"]),
        "updated_at": epoch_ms_iso(session["updated_ms"]),
        "is_subagent": False,
    }


def no_metadata(_path: Path, _thread_names):
    return None


def no_turn(_item):
    return None


ArchiveFormat = namedtuple("ArchiveFormat", ("metadata", "turn", "record_time"), defaults=(item_time,))

ARCHIVE_FORMATS = {
    "codex": ArchiveFormat(codex_metadata, codex_turn, codex_time),
    "claude": ArchiveFormat(claude_metadata, claude_turn),
    "omp": ArchiveFormat(omp_metadata, omp_turn),
    "opencode": ArchiveFormat(opencode_metadata, opencode_turn),
}
UNKNOWN_FORMAT = ArchiveFormat(no_metadata, no_turn)


def archive_format(agent: str) -> ArchiveFormat:
    kind = app_config()["archive"].get(agent, {}).get("kind")
    return ARCHIVE_FORMATS.get(kind) or ARCHIVE_FORMATS.get(agent, UNKNOWN_FORMAT)


def archive_path_timestamp(agent: str, path: Path) -> float:
    session = OPENCODE_SOURCES.get(str(path))
    if session is not None:
        return session["created_ms"] / 1000
    match = re.search(r"(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})(-\d{3}Z)?", path.name)
    if match:
        try:
            started = datetime.strptime(match.group(1), "%Y-%m-%dT%H-%M-%S")
            # Codex names its files in local time; omp's milliseconds-and-Z names are UTC.
            return (started.replace(tzinfo=timezone.utc) if match.group(2) else started).timestamp()
        except ValueError:
            pass
    record_time = archive_format(agent).record_time
    try:
        for item in iter_archive_records(path):
            parsed = iso_to_epoch(record_time(item))
            if parsed:
                return parsed
    except OSError:
        pass
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def archive_file_metadata(agent: str, path: Path, thread_names):
    try:
        found = archive_format(agent).metadata(path, thread_names)
    except OSError:
        return None
    if found is None:
        return None
    started_epoch = archive_path_timestamp(agent, path)
    started_at = datetime.fromtimestamp(started_epoch).isoformat() if started_epoch else ""
    return {"agent": agent, "path": str(path), "started_at": started_at, "updated_at": started_at, **found}


def archive_catalog_turn(agent: str, item):
    role, content = archive_format(agent).turn(item) or ("", None)
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
