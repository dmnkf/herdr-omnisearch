"""Where a session runs on this machine, in the herdr TUI or in herdr-core, read from both live indexes."""

import json
import os
import shutil
import time

from .live_index import FRONTEND_KEY, index_session
from .settings import CORE_SESSION_PREFIX, DEFAULT_LINES, core_session_key
from .storage import app_db_path, connect, db_path, exclusive_lock

START_GRACE_SECONDS = 30


def frontend_records():
    """Each indexed session's record on this machine, with the frontend it belongs to."""
    records = []
    for frontend, path in (("core", app_db_path()), ("tui", db_path())):
        conn = connect(path)
        try:
            rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE ?", (f"{FRONTEND_KEY}%",)).fetchall()
        finally:
            conn.close()
        for key, value in rows:
            session = key[len(FRONTEND_KEY):]
            if (frontend == "core") != session.startswith(CORE_SESSION_PREFIX):
                continue
            if frontend == "core" and session != core_session_key():
                continue
            records.append({"frontend": frontend, "session": session, **json.loads(value)})
    return records


def mirror_of(records, frontend: str, target: str):
    """The pane or terminal of the other frontend that mirrors this one, as (target, socket)."""
    origin = f"{frontend}:{target}"
    for record in records:
        if record["frontend"] != frontend and origin in record.get("mirrors", {}):
            return record["mirrors"][origin], record.get("socket") or ""
    return None, ""


def location(record, owner, records):
    mirror, mirror_socket = mirror_of(records, record["frontend"], owner["pane_id"])
    return {**owner, "frontend": record["frontend"], "socket": record.get("socket") or "",
            "mirror": mirror, "mirror_socket": mirror_socket}


def running_sessions():
    """Every session running here, by (agent, session id), with each pane or terminal running it; mirrors never count."""
    records = frontend_records()
    found = {}
    for record in records:
        for owner in record.get("owners", []):
            found.setdefault((owner["agent"], owner["session_id"]), []).append(location(record, owner, records))
    return found


def session_locations(agent: str, session_id: str):
    return running_sessions().get((agent, session_id), [])


def pick_location(locations, frontend: str):
    """The one to focus from `frontend`: its own, else one it mirrors, else any."""
    for good in (lambda found: found["frontend"] == frontend, lambda found: found["mirror"]):
        found = next((place for place in locations if good(place)), None)
        if found:
            return found
    return locations[0] if locations else None


def core_mirror(address: str):
    """The Herdr pane mirroring a herdr-core terminal here, as (pane, socket)."""
    return mirror_of(frontend_records(), "core", address)


def core_title(address: str) -> str:
    """What the app calls a herdr-core terminal here, when it runs a session."""
    owners = (owner for record in frontend_records() if record["frontend"] == "core" for owner in record.get("owners", []))
    return next((owner["title"] for owner in owners if owner["pane_id"] == address and owner["title"]), "")


def refresh_frontends(launchers=(), *, core=False):
    """Re-read what both frontends run here, herdr-core always when core; errors say which could not be read."""
    from .archive_catalog import archive_catalog_session_agents
    from .herdr_core import CoreError, index_core
    from .herdr_cli import HerdrCLIError
    from .herdr_socket import HerdrError, resolve_socket_path, socket_is_alive
    errors = []
    if core or shutil.which("hc"):
        try:
            index_core(archive_catalog_session_agents, launchers)
        except CoreError as exc:
            errors.append(str(exc))
    if socket_is_alive(resolve_socket_path()):
        try:
            index_session(DEFAULT_LINES, False, False)
        except (HerdrError, HerdrCLIError) as exc:
            errors.append(f"herdr: {exc}")
    return errors


def claim_start(agent: str, session_id: str, frontend: str):
    """Where the session runs, or "starting" when another start claimed it lately; None reserves it for this start."""
    # Beside the indexes it reads, which is the state directory unless a test moves them.
    starts_dir = app_db_path().expanduser().parent
    lock = exclusive_lock(starts_dir / "session-starts.lock")
    try:
        found = pick_location(session_locations(agent, session_id), frontend)
        if found:
            return found
        path = starts_dir / "session-starts.json"
        try:
            starts = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            starts = {}
        now = time.time()
        starts = {key: at for key, at in starts.items() if now - at < START_GRACE_SECONDS}
        key = f"{agent}\0{session_id}"
        if key in starts:
            return "starting"
        starts[key] = now
        path.write_text(json.dumps(starts), encoding="utf-8")
        return None
    finally:
        os.close(lock)
