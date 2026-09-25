import re
import shlex
import sys
from pathlib import Path

from .herdr_cli import HerdrCLI, HerdrCLIError
from .herdr_socket import HerdrClient, HerdrError, resolve_socket_path
from .machines import MachineError, focus_remote_row
from .settings import app_config, herdr_bin
from .storage import connect
from .textmatch import shorten
from .live_index import is_workspace_row, machine_name, merge_agent_records, pane_agent
from .archive_catalog import archive_catalog_result, archive_space_label, is_archive_placeholder_label

def focused_pane_id(client=None) -> str:
    return (client or HerdrClient()).snapshot().get("focused_pane_id") or ""


def row_socket_path(row):
    """Return the row's socket when it belongs to a different Herdr session."""
    socket_path = row.get("socket_path") or ""
    if socket_path and socket_path != resolve_socket_path():
        return socket_path
    return None


def row_client(row) -> HerdrClient:
    return HerdrClient(socket_path=row_socket_path(row))


def row_agent_cli(row) -> HerdrCLI:
    socket_path = row_socket_path(row)
    if socket_path:
        return HerdrCLI(herdr_bin(), env={"HERDR_SOCKET_PATH": socket_path})
    return HerdrCLI(herdr_bin())


def focus_workspace_tab(row) -> bool:
    client = row_client(row)
    try:
        if row.get("workspace_id"):
            client.focus_workspace(row["workspace_id"])
        if row.get("tab_id"):
            client.focus_tab(row["tab_id"])
        return True
    except HerdrError:
        return False


def focus_exact_pane(row) -> bool:
    pane_id = row.get("pane_id") or ""
    if not pane_id or is_workspace_row(row):
        return False
    try:
        if pane_agent(row):
            try:
                row_agent_cli(row).agent_focus(pane_id)
            except HerdrCLIError:
                row_client(row).focus_pane(pane_id)
        else:
            row_client(row).focus_pane(pane_id)
        return focused_pane_id(row_client(row)) == pane_id
    except HerdrError:
        return False


def focus_result(stable_id: str) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM docs WHERE stable_id = ?", (stable_id,)).fetchone()
    conn.close()
    if not row:
        print(f"unknown result id: {stable_id}", file=sys.stderr)
        return 2
    return focus_row(dict(row))


def focus_row(row) -> int:
    if row.get("machine_id"):
        return focus_machine_row(row)
    if is_workspace_row(row):
        return 0 if focus_workspace_tab(row) else 1
    if focus_exact_pane(row):
        return 0
    print(f"could not focus exact pane: {row.get('pane_id') or row.get('stable_id')}", file=sys.stderr)
    return 1


def focus_machine_row(row) -> int:
    try:
        focus_remote_row(row)
    except MachineError as exc:
        print(f"could not focus on {machine_name(row)}: {exc}", file=sys.stderr)
        return 1
    # Herdr gives other processes no way to switch the client's machine, so say where to go.
    try:
        HerdrClient().show_notification(
            f"OmniSearch: {machine_name(row)} › {row.get('workspace_label') or row.get('workspace_id')}",
            f"Ready on {machine_name(row)}. Select it in the sidebar or with prefix+w to land there.",
        )
    except HerdrError:
        pass
    return 0


def focus_target(workspace_id: str, tab_id: str, pane_id: str, agent: str, workspace_only: bool) -> int:
    row = {"workspace_id": workspace_id, "tab_id": tab_id, "pane_id": pane_id, "agent": agent}
    if workspace_only or not pane_id:
        return 0 if focus_workspace_tab(row) else 1
    focus_workspace_tab(row)
    if focus_exact_pane(row):
        return 0
    print(f"could not focus exact pane: {pane_id}", file=sys.stderr)
    return 1


def archive_resume_command(row):
    fallback_cwd = str(Path(app_config()["fallback_cwd"]).expanduser())
    cwd = row.get("cwd") or fallback_cwd
    if not Path(cwd).exists():
        cwd = fallback_cwd
    session_id = row["session_id"]
    agent_cfg = app_config()["archive"].get(row["agent"], {})
    template = agent_cfg.get("resume") or ""
    if not template:
        raise RuntimeError(f"cannot resume archive agent without configured command: {row['agent']}")
    context = {
        "agent": row["agent"],
        "cwd": cwd,
        "session_id": session_id,
        "title": row.get("title") or "",
        "path": row.get("path") or "",
    }
    # Tokenize before substitution so values with spaces or shell
    # metacharacters stay single arguments instead of splitting the command.
    return cwd, [token.format(**context) for token in shlex.split(template)]


def archive_agent_start(row, pane_id: str, command):
    agent_cfg = app_config()["archive"].get(row["agent"], {})
    kind = agent_cfg.get("kind") or row["agent"]
    executable = Path(command[0]).name if command else ""
    if executable != kind:
        raise RuntimeError(
            f"native launcher for {row['agent']} requires a {kind} resume command; "
            "set launcher = shell for wrapper commands"
        )
    raw_name = f"resume-{kind}-{row['session_id'][:12]}".lower()
    name = re.sub(r"[^a-z0-9_-]+", "-", raw_name).strip("-_")[:32]
    if not name or not name[0].isalpha():
        name = f"resume-{row['session_id'][:12]}"[:32]
    timeout_ms = int(agent_cfg.get("start_timeout_ms") or 60000)
    return HerdrCLI(herdr_bin()).agent_start(
        name,
        kind,
        pane_id,
        command[1:],
        timeout_ms=timeout_ms,
    )


def launch_archive_in_pane(client, row, pane_id: str, command):
    launcher = app_config()["archive"].get(row["agent"], {}).get("launcher") or "agent"
    if launcher == "agent":
        return archive_agent_start(row, pane_id, command)
    if launcher == "shell":
        return client.send_input(pane_id, shlex.join(command))
    raise RuntimeError(f"unsupported archive launcher for {row['agent']}: {launcher}")


def archive_resume_label(row) -> str:
    title = row.get("title") or row.get("session_id") or "session"
    space = row.get("space_label") or ""
    if is_archive_placeholder_label(space):
        space = ""
    space = space or archive_space_label(row)
    if not space or is_archive_placeholder_label(space):
        return title
    if title and title.lower() != space.lower() and space.lower() not in title.lower():
        return f"{space} / {title}"
    return title or space


def focus_pane_row(pane) -> bool:
    pane_id = pane.get("pane_id")
    if not pane_id:
        return False
    return focus_exact_pane(pane)


def pane_matches_archive_session(pane, row, workspace_label: str = "") -> bool:
    session_id = row.get("session_id") or ""
    agent = row.get("agent") or ""
    if not session_id:
        return False

    pane_session = pane.get("agent_session") or {}
    pane_session_id = pane_session.get("value") or ""
    pane_session_agent = pane_session.get("agent") or pane.get("agent") or ""
    if pane_session_id == session_id:
        return not agent or not pane_session_agent or pane_session_agent == agent

    # Fallback for panes that have started but have not reported agent_session yet.
    session_prefix = session_id[:8]
    label_text = " ".join(
        [
            pane.get("label") or "",
            workspace_label or "",
        ]
    ).lower()
    if session_prefix and session_prefix in label_text:
        return not agent or pane.get("agent") in (agent, None, "")
    return False


def find_existing_archive_pane(row):
    snapshot = HerdrClient().snapshot()
    panes_payload = snapshot.get("panes", [])
    try:
        panes_payload = merge_agent_records(
            panes_payload,
            HerdrCLI(herdr_bin()).agent_list(),
        )
    except HerdrCLIError:
        panes_payload = merge_agent_records(panes_payload, [])
    workspace_labels = {
        workspace["workspace_id"]: workspace.get("label") or workspace["workspace_id"]
        for workspace in snapshot.get("workspaces", [])
    }

    for pane in panes_payload:
        workspace_label = workspace_labels.get(pane.get("workspace_id"), "")
        if pane_matches_archive_session(pane, row, workspace_label):
            return pane
    return None


def focus_archive_catalog_result(stable_id: str) -> int:
    row = archive_catalog_result(stable_id)
    if not row:
        print(f"unknown archive catalog result id: {stable_id}", file=sys.stderr)
        return 2
    return focus_archive_row(row)


def focus_archive_row(row) -> int:
    try:
        pane = find_existing_archive_pane(row)
    except RuntimeError:
        pane = None
    if pane:
        if focus_pane_row(pane):
            return 0
        print(f"found existing archive pane but could not focus exact pane: {pane.get('pane_id')}", file=sys.stderr)
        return 1
    cwd, command = archive_resume_command(row)
    title = row.get("title") or row["session_id"]
    session_prefix = row["session_id"][:8]
    label = shorten(archive_resume_label(row), 48)
    agent_name = shorten(f"{row['agent']} {session_prefix} {title}", 44)
    try:
        client = HerdrClient()
        created = client.create_workspace(cwd, label, focus=True)
        root_pane = created["root_pane"]["pane_id"]
        client.rename_pane(root_pane, agent_name)
        launch_archive_in_pane(client, row, root_pane, command)
        return 0
    except (HerdrError, HerdrCLIError, KeyError, RuntimeError) as exc:
        print(f"archive resume failed: {exc}", file=sys.stderr)
        return 1
