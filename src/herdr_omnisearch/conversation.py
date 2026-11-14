"""An agent session's messages, for the Herdr app mod's preview.

The catalog says where a session's history file is and finds older matches. The end of
the file itself is read on every call, so a live agent's newest messages show at once
instead of after the next catalog refresh.
"""
import json
from pathlib import Path

from .archive_sources import archive_catalog_turn, is_opencode_source
from .storage import archive_catalog_connect

MESSAGE_CHARS = 6000
TAIL_BYTES = 2 * 1024 * 1024


def tail_turns(agent: str, path: Path):
    """Messages in the last TAIL_BYTES of a JSON-lines history file, oldest first."""
    size = path.stat().st_size
    with path.open("rb") as handle:
        handle.seek(max(0, size - TAIL_BYTES))
        data = handle.read()
    lines = data.split(b"\n")
    if size > TAIL_BYTES:
        lines = lines[1:]
    turns = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        turn = archive_catalog_turn(agent, item) if isinstance(item, dict) else None
        if turn:
            turns.append((turn["role"], turn["message_epoch"], turn["content"]))
    return turns


def window(turns, words, limit):
    """The last `limit` turns, or the ones up to just after the newest holding a word; None when searched and absent."""
    lowered = [w.lower() for w in words if w]
    end, match = len(turns), None
    if lowered:
        for i in range(len(turns) - 1, -1, -1):
            if any(w in (turns[i][2] or "").lower() for w in lowered):
                match, end = i, min(len(turns), i + 2)
                break
        else:
            return None
    start = max(0, end - limit)
    return [
        {"role": role, "at": epoch or 0, "text": (text or "")[:MESSAGE_CHARS], "match": start + i == match}
        for i, (role, epoch, text) in enumerate(turns[start:end])
    ]


def conversation(agent: str, session_id: str, words, limit: int = 12):
    conn = archive_catalog_connect()
    try:
        session = conn.execute(
            "SELECT session_key, path FROM catalog_sessions WHERE agent = ? AND session_id = ? "
            "ORDER BY is_present DESC, updated_epoch DESC LIMIT 1",
            (agent, session_id),
        ).fetchone()
        if session is None:
            return {"found": False, "messages": [], "error": None}
        session_key, path = session[0], Path(session[1] or "")
        if not is_opencode_source(agent) and path.is_file():
            try:
                fresh = window(tail_turns(agent, path), words, limit)
            except OSError:
                fresh = None
            if fresh is not None:
                return {"found": True, "messages": fresh, "error": None}
        rows = [tuple(r) for r in conn.execute(
            "SELECT role, message_epoch, content FROM catalog_messages WHERE session_key = ? ORDER BY ordinal",
            (session_key,),
        ).fetchall()]
    finally:
        conn.close()
    return {"found": True, "messages": window(rows, words, limit) or window(rows, [], limit), "error": None}
