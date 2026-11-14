from .archive_catalog import (
    archive_catalog_live_matches,
    archive_catalog_search,
    maybe_background_archive_catalog_index,
)
from .live_index import grouped_search_index, is_workspace_row
from .navigate import archive_resume_command
from .running import claim_start, frontend_records, mirror_of, pick_location, refresh_frontends, running_sessions
from .settings import app_config, core_session_key
from .storage import app_db_path, db_path

CATALOG_STALE_SECONDS = 60


def terminal_row(row, frontend):
    return {
        "kind": "terminal",
        "frontend": frontend,
        "address": row["pane_id"],
        "space": row.get("workspace_id") or "",
        "tab": row.get("tab_id") or "",
        "title": row.get("pane_label") or "",
        "agent": row.get("agent") or "",
        "status": row.get("agent_status") or "unknown",
        "snippet": " ".join((row.get("snippet") or "").split()),
        "cwd": row.get("foreground_cwd") or row.get("cwd") or "",
        "session_id": row.get("agent_session_id") or "",
    }


def live_rows(query: str, limit: int):
    """herdr-core terminals with their spaces, then the herdr TUI's agent panes on this machine."""
    out = []
    for row in grouped_search_index(
        query, limit, session=core_session_key(), session_matcher=archive_catalog_live_matches, db=app_db_path(),
    ):
        if is_workspace_row(row):
            out.append({"kind": "space", "space": row["workspace_id"], "name": row.get("workspace_label") or ""})
        else:
            out.append(terminal_row(row, "core"))
    records = frontend_records()
    for row in grouped_search_index(
        query, limit, all_sessions=True, session_matcher=archive_catalog_live_matches, db=db_path(),
    ):
        if not is_workspace_row(row):
            out.append({
                **terminal_row(row, "tui"),
                "space_name": row.get("workspace_label") or "",
                "socket": row.get("socket_path") or "",
                "mirror": mirror_of(records, "tui", row["pane_id"])[0],
            })
    return out


def location_row(place):
    """A pane or terminal running a session, as the mod focuses it: a herdr-core address, or a TUI pane and its mirror."""
    tui = place["frontend"] == "tui"
    return {
        "kind": "terminal",
        "frontend": place["frontend"],
        "address": place["pane_id"],
        "space": place.get("workspace_id") or "",
        "tab": place.get("tab_id") or "",
        "title": place.get("title") or "",
        "agent": place.get("agent") or "",
        "space_name": place.get("workspace_label") or "",
        "socket": place["socket"] if tui else "",
        "mirror": place["mirror"] if tui else None,
    }


def session_rows(query: str, limit: int):
    running = running_sessions()
    out = []
    for row in archive_catalog_search(query, limit):
        try:
            folder, argv = archive_resume_command(row)
        except (RuntimeError, ValueError, KeyError):
            folder, argv = row.get("cwd") or "", None
        place = pick_location(running.get((row["agent"], row["session_id"]), []), "core")
        where = location_row(place) if place else None
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
            # The herdr-core terminal to focus, for mods that know no other frontend.
            "address": (where["address"] if where["frontend"] == "core" else where["mirror"]) if where else None,
            "running": where,
        })
    return out


def running_rows(agent: str, session_id: str, errors):
    """The pane or terminal running a session; when none does, reserve its start so no other start runs it too."""
    if errors:
        return []
    place = claim_start(agent, session_id, "core")
    if place == "starting":
        errors.append(f"{agent} session {session_id[:8]} is already starting")
        return []
    return [location_row(place)] if place else []


def app_search(query: str, *, archive: bool, refresh: bool, limit: int, launchers=(), running=None):
    """Rows for the Herdr app's mod: live terminals and TUI panes, or old sessions; launchers are its (agent, argv) pairs."""
    errors = []
    if refresh:
        errors.extend(refresh_frontends(launchers, core=True))
        if app_config()["archive_enabled"]:
            maybe_background_archive_catalog_index("", CATALOG_STALE_SECONDS)
    if running:
        rows = running_rows(*running, errors)
    elif archive and not app_config()["archive_enabled"]:
        errors.append("archive search is off: set enabled = true under [archive] in OmniSearch's config.ini")
        rows = []
    else:
        rows = session_rows(query, limit) if archive else live_rows(query, limit)
    return {"rows": rows, "error": "; ".join(errors) or None}
