from .archive_catalog import (
    archive_catalog_live_matches,
    archive_catalog_search,
    archive_catalog_session_agents,
    maybe_background_archive_catalog_index,
)
from .herdr_core import CoreError, index_core
from .live_index import grouped_search_index, is_workspace_row
from .navigate import archive_resume_command
from .settings import app_config, core_session_key
from .storage import app_db_path, connect

CATALOG_STALE_SECONDS = 60


def live_rows(query: str, limit: int):
    rows = grouped_search_index(
        query, limit, session=core_session_key(), session_matcher=archive_catalog_live_matches, db=app_db_path(),
    )
    out = []
    for row in rows:
        if is_workspace_row(row):
            out.append({"kind": "space", "space": row["workspace_id"], "name": row.get("workspace_label") or ""})
            continue
        out.append({
            "kind": "terminal",
            "address": row["pane_id"],
            "space": row.get("workspace_id") or "",
            "tab": row.get("tab_id") or "",
            "title": row.get("pane_label") or "",
            "agent": row.get("agent") or "",
            "status": row.get("agent_status") or "unknown",
            "snippet": " ".join((row.get("snippet") or "").split()),
            "cwd": row.get("foreground_cwd") or row.get("cwd") or "",
        })
    return out


def live_addresses():
    conn = connect(app_db_path())
    try:
        return {
            (row["agent"], row["agent_session_id"]): row["pane_id"]
            for row in conn.execute(
                "SELECT agent, agent_session_id, pane_id FROM docs WHERE herdr_session = ? AND COALESCE(agent_session_id, '') <> ''",
                (core_session_key(),),
            )
        }
    finally:
        conn.close()


def session_rows(query: str, limit: int):
    running = live_addresses()
    out = []
    for row in archive_catalog_search(query, limit):
        try:
            folder, argv = archive_resume_command(row)
        except (RuntimeError, ValueError, KeyError):
            folder, argv = row.get("cwd") or "", None
        out.append({
            "kind": "session",
            "id": row["stable_id"],
            "agent": row["agent"],
            "session_id": row["session_id"],
            "title": row.get("title") or "",
            "space_name": row.get("workspace_label") or "",
            "updated": row.get("updated_epoch") or 0,
            "snippet": " ".join((row.get("snippet") or "").split()),
            "folder": folder,
            "argv": argv,
            "address": running.get((row["agent"], row["session_id"])),
        })
    return out


def app_search(query: str, *, archive: bool, refresh: bool, limit: int, launchers=(), running=None):
    """Rows for the Herdr app's mod: herdr-core terminals, or old sessions; launchers are its (agent, argv) pairs."""
    errors = []
    if refresh:
        try:
            index_core(archive_catalog_session_agents, launchers)
        except CoreError as exc:
            errors.append(str(exc))
        if app_config()["archive_enabled"]:
            maybe_background_archive_catalog_index("", CATALOG_STALE_SECONDS)
    if running:
        address = live_addresses().get(running)
        rows = [{"kind": "terminal", "address": address}] if address else []
    elif archive and not app_config()["archive_enabled"]:
        errors.append("archive search is off: set enabled = true under [archive] in OmniSearch's config.ini")
        rows = []
    else:
        rows = session_rows(query, limit) if archive else live_rows(query, limit)
    return {"rows": rows, "error": "; ".join(errors) or None}
