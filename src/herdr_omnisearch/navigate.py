import re
import shlex
import sys
from pathlib import Path

from .herdr_cli import HerdrCLI, HerdrCLIError
from .herdr_socket import HerdrClient, HerdrError, resolve_socket_path
from .machines import MachineError, focus_remote_row, resume_remote_row
from .running import claim_start, core_mirror, core_title, refresh_frontends
from .settings import app_config, herdr_bin
from .storage import app_db_path, connect, db_path
from .textmatch import shorten
from .live_index import is_workspace_row, machine_name, pane_agent, row_frontend
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
    for path in (db_path(), app_db_path()):
        conn = connect(path)
        row = conn.execute("SELECT * FROM docs WHERE stable_id = ?", (stable_id,)).fetchone()
        conn.close()
        if row:
            return focus_row(dict(row))
    print(f"unknown result id: {stable_id}", file=sys.stderr)
    return 2


def say(message: str) -> None:
    """Tell the person where something is: on stdout for a machine that asked over SSH, and in Herdr."""
    print(message)
    try:
        HerdrClient().show_notification("OmniSearch", message)
    except HerdrError:
        pass


def focus_core_terminal(address: str, title: str = "") -> int:
    """Focus the Herdr pane mirroring a herdr-core terminal, or say where it runs."""
    pane_id, socket_path = core_mirror(address)
    if pane_id and focus_exact_pane({"pane_id": pane_id, "socket_path": socket_path}):
        return 0
    say(f"{title or core_title(address) or address} runs in the Herdr app as {address}; no Herdr pane mirrors it.")
    return 0


def focus_row(row) -> int:
    if row_frontend(row) == "core" and is_workspace_row(row):
        machine = f" on {machine_name(row)}" if row.get("machine_id") else ""
        say(f"{row.get('workspace_label') or row.get('workspace_id')} is a space in the Herdr app{machine}.")
        return 0
    if row.get("machine_id"):
        return focus_machine_row(row)
    if row_frontend(row) == "core":
        return focus_core_terminal(row["pane_id"], row.get("pane_label") or "")
    if is_workspace_row(row):
        return 0 if focus_workspace_tab(row) else 1
    if focus_exact_pane(row):
        return 0
    print(f"could not focus exact pane: {row.get('pane_id') or row.get('stable_id')}", file=sys.stderr)
    return 1


def focus_machine_row(row) -> int:
    try:
        said = focus_remote_row(row)
    except MachineError as exc:
        print(f"could not focus on {machine_name(row)}: {exc}", file=sys.stderr)
        return 1
    tell_ready(row, said)
    return 0


def tell_ready(row, said: str) -> None:
    """Relay what a machine said it did, else where to go: Herdr gives no way to switch the client's machine."""
    try:
        HerdrClient().show_notification(
            f"OmniSearch: {machine_name(row)} › {row.get('workspace_label') or row.get('workspace_id')}",
            f"{machine_name(row)}: {said}" if said else
            f"Ready on {machine_name(row)}. Select it in the sidebar or with prefix+w to land there.",
        )
    except HerdrError:
        pass


def focus_target(workspace_id: str, tab_id: str, pane_id: str, agent: str, workspace_only: bool, socket_path: str = "",
                 core: str = "") -> int:
    if core:
        return focus_core_terminal(core)
    row = {"workspace_id": workspace_id, "tab_id": tab_id, "pane_id": pane_id, "agent": agent, "socket_path": socket_path}
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


def focus_archive_catalog_result(stable_id: str) -> int:
    row = archive_catalog_result(stable_id)
    if not row:
        print(f"unknown archive catalog result id: {stable_id}", file=sys.stderr)
        return 2
    return focus_archive_row(row)


def focus_running(place) -> int:
    """Focus a pane or terminal running a session, from Herdr."""
    if place["frontend"] == "core":
        return focus_core_terminal(place["pane_id"], place.get("title") or "")
    if focus_exact_pane({"pane_id": place["pane_id"], "agent": place.get("agent"), "socket_path": place["socket"]}):
        return 0
    print(f"found the running session but could not focus pane: {place['pane_id']}", file=sys.stderr)
    return 1


def focus_archive_row(row) -> int:
    if row.get("machine_id"):
        return resume_on_machine(row)
    errors = refresh_frontends()
    if errors:
        print(f"not resumed, could not check where it runs: {'; '.join(errors)}", file=sys.stderr)
        return 1
    place = claim_start(row["agent"], row["session_id"], "tui")
    if place == "starting":
        say(f"{row['agent']} session {row['session_id'][:8]} is already starting.")
        return 0
    if place:
        return focus_running(place)
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


def resume_on_machine(row) -> int:
    """Resume, or focus, a session of a saved machine there, with that machine's own commands and lookup."""
    try:
        said = resume_remote_row(row)
    except MachineError as exc:
        print(f"could not resume on {machine_name(row)}: {exc}", file=sys.stderr)
        return 1
    tell_ready(row, said)
    return 0
