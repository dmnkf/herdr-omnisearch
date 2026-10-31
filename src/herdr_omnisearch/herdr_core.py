import json
import re
import subprocess

from .live_index import STATUS_WEIGHT, claude_sessions, store_session
from .settings import core_session_key
from .storage import app_db_path

HC_TIMEOUT_SECONDS = 10
AGENTS = {"claude", "codex", "opencode", "omp", "pi", "droid", "gemini", "amp"}
REPORTED_APPS = {"claude-code": "claude"}
CLAUDE_STATUS = {"busy": "working", "waiting": "blocked", "idle": "idle"}
REPORTED_STATES = {"error": "blocked"}
RESUME_FLAGS = {"--resume", "-r", "--session", "-s", "--session-id"}
TITLE_MARKS = re.compile(r"^[^\w(\[]+")


class CoreError(RuntimeError):
    pass


def hc(*args) -> str:
    try:
        result = subprocess.run(
            ["hc", *args], capture_output=True, text=True, timeout=HC_TIMEOUT_SECONDS, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CoreError(f"hc {args[0]} failed: {exc}") from exc
    if result.returncode != 0:
        raise CoreError(f"hc {args[0]} failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


def core_terminals():
    """This machine's live terminals; a connected machine's (`vps/t7`) index themselves there."""
    terminals = []
    for entry in json.loads(hc("ls", "--json")).get("terminals", []):
        if "/" in entry["address"]:
            continue
        try:
            terminal = json.loads(hc("get", entry["address"], "--json"))
        except CoreError:
            continue
        if terminal.get("state") == "live":
            terminals.append(terminal)
    return terminals


def screen_text(address: str) -> str:
    # History of a full-screen app is read by scrolling it, under the person's hands.
    return json.loads(hc("read", address, "--lines", "0", "--json"))["text"]


def reported_status(terminal):
    """The program's OSC 7501 report: state, app, title."""
    status = terminal.get("status")
    return status if isinstance(status, dict) else {}


def terminal_agent(terminal) -> str:
    app = reported_status(terminal).get("app")
    if app:
        return REPORTED_APPS.get(app, app)
    argv = terminal.get("argv") or [""]
    programs = ((terminal.get("foreground") or {}).get("name") or "", argv[0].rsplit("/", 1)[-1])
    return next((program.lower() for program in programs if program.lower() in AGENTS), "")


def resumed_session_id(argv) -> str:
    """The session a command resumes: a resume flag's value, or the last word after `resume`."""
    for flag, value in zip(argv, argv[1:]):
        if flag in RESUME_FLAGS and not value.startswith("-"):
            return value
    if "resume" in argv[1:3] and not argv[-1].startswith("-") and argv[-1] != "resume":
        return argv[-1]
    return ""


def terminal_pane(terminal, labels, sessions_by_pid):
    foreground = terminal.get("foreground") or {}
    claude = sessions_by_pid.get(foreground.get("pid")) or sessions_by_pid.get(terminal.get("pid")) or {}
    report = reported_status(terminal)
    status = REPORTED_STATES.get(report.get("state"), report.get("state"))
    if status not in STATUS_WEIGHT:
        status = CLAUDE_STATUS.get(claude.get("status"), "unknown")
    session_id = (
        claude.get("sessionId")
        or resumed_session_id(foreground.get("argv") or [])
        or resumed_session_id(terminal.get("argv") or [])
    )
    address = terminal["address"]
    return {
        "workspace_id": labels.get("herdr.space", ""),
        "tab_id": labels.get("herdr.tab", ""),
        "pane_id": address,
        "terminal_id": address,
        "label": terminal.get("name") or "",
        "terminal_title_stripped": TITLE_MARKS.sub("", report.get("title") or terminal.get("title") or "").strip(),
        "agent": terminal_agent(terminal),
        "agent_session": {"value": session_id} if session_id else None,
        "agent_status": status,
        "cwd": terminal.get("cwd") or "",
        "foreground_cwd": foreground.get("cwd") or "",
    }


def shown_labels(terminal, by_address):
    """The Herdr labels placing a terminal, a sub-agent taking its parent's; None for terminals Herdr does not show."""
    seen = set()
    while terminal and terminal["address"] not in seen:
        labels = terminal.get("labels") or {}
        if labels.get("herdr.space"):
            return labels
        seen.add(terminal["address"])
        terminal = by_address.get(terminal.get("parent"))
    return None


def store_core(spaces, panes, screens) -> int:
    return store_session(
        core_session_key(), "", spaces, panes, lambda pane: screens[pane["pane_id"]],
        include_empty=False, include_wrappers=False, db=app_db_path(),
    )


def index_core() -> int:
    """Index the terminals the Herdr app shows on this machine, in the app's own index."""
    try:
        terminals = core_terminals()
    except CoreError:
        # A core that is gone took its terminals with it.
        store_core([], [], {})
        raise
    by_address = {terminal["address"]: terminal for terminal in terminals}
    sessions_by_pid = {record.get("pid"): record for record in claude_sessions()}
    spaces = {}
    panes = []
    screens = {}
    for terminal in terminals:
        labels = shown_labels(terminal, by_address)
        if labels is None:
            continue
        try:
            screens[terminal["address"]] = screen_text(terminal["address"])
        except CoreError:
            continue
        space_name = (labels.get("where") or "").split(" / ")[0]
        spaces.setdefault(labels["herdr.space"], {
            "workspace_id": labels["herdr.space"],
            "label": space_name or labels["herdr.space"],
            "active_tab_id": labels.get("herdr.tab", ""),
        })
        panes.append(terminal_pane(terminal, labels, sessions_by_pid))
    return store_core(list(spaces.values()), panes, screens)
