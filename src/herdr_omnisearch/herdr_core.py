import json
import re
import shlex
import subprocess
from pathlib import Path

from .herdr_programs import INTERPRETERS, KNOWN_PROGRAMS, SHELLS
from .live_index import STATUS_WEIGHT, claude_sessions, store_session
from .settings import app_config, core_session_key
from .storage import app_db_path

HC_TIMEOUT_SECONDS = 10
CLAUDE_STATUS = {"busy": "working", "waiting": "blocked", "idle": "idle"}
REPORTED_STATES = {"error": "blocked"}
TITLE_MARKS = re.compile(r"^[^\w(\[]+")
SCRIPT_EXTENSIONS = (".js", ".mjs", ".cjs", ".ts", ".py", ".rb")
FISH_LAUNCH = "command $argv[2..]; exec $argv[1] -"


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


def agent_names(launchers=()):
    """Agents by program name: the ones OmniSearch catalogs, their resume commands', and the app's launchers."""
    config = app_config()
    names = {}
    programs = [(agent, agent) for agent in config["archive_agents"]]
    for agent in config["archive_agents"]:
        resume = unwrap_launch(shlex.split(config["archive"].get(agent, {}).get("resume") or ""))
        programs += [(agent, resume[0])] if resume else []
    for agent, program in [*programs, *launchers]:
        name = program_name(program)
        if name and name not in SHELLS | INTERPRETERS:
            names.setdefault(name, agent)
    return names


def program_name(word: str) -> str:
    # A login shell's argv[0] is `-zsh`.
    return Path(word).name.lstrip("-").lower()


def unwrap_launch(argv):
    """The command inside the app's `sh -c '"$@"; exec "$0" -i' sh <command>` wrapper, or argv itself."""
    while (
        len(argv) > 4 and program_name(argv[0]) in SHELLS and argv[1].startswith("-") and argv[1].endswith("c")
        and ('"$@"' in argv[2] or argv[2].startswith(FISH_LAUNCH))
    ):
        argv = argv[4:]
    return argv


def program_agent(argv, names) -> str:
    """The agent a command runs, read as Herdr's program table reads it (herdr-app src/programs.rs)."""
    argv = unwrap_launch(list(argv))
    if not argv:
        return ""
    first = program_name(argv[0])
    if first in INTERPRETERS:
        # The first program Herdr knows that the interpreter runs: an agent, or something else.
        for word in argv[1:]:
            if word.startswith("-"):
                continue
            name = program_name(word)
            stem = next((name[: -len(ext)] for ext in SCRIPT_EXTENSIONS if name.endswith(ext)), name)
            if stem in names:
                return names[stem]
            if stem in KNOWN_PROGRAMS:
                return ""
    return names.get(first, "")


def running_argv(terminal):
    foreground = terminal.get("foreground")
    # Not its launch command: an agent the app started leaves a shell behind when it exits.
    return (foreground.get("argv") or []) if foreground else terminal.get("argv") or []


def command_words(terminal):
    """Every word of what runs in the terminal now, and the value of each `--flag=value`."""
    return [part for word in running_argv(terminal) for part in (word, word.partition("=")[2]) if part]


def live_claude_sessions():
    """Claude Code's running sessions by its pid and its parent's: the app starts agents under a shell."""
    records = {record["pid"]: record for record in claude_sessions() if isinstance(record.get("pid"), int)}
    if not records:
        return {}
    try:
        listing = subprocess.run(
            ["ps", "-o", "pid=,ppid=", "-p", ",".join(map(str, records))],
            capture_output=True, text=True, timeout=HC_TIMEOUT_SECONDS, check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return records
    parents = dict(tuple(map(int, line.split())) for line in listing.splitlines() if len(line.split()) == 2)
    # A file left by an ended session would claim whatever process reuses its pid.
    live = {pid: records[pid] for pid in parents if pid in records}
    for pid, parent in parents.items():
        if pid in records:
            live.setdefault(parent, records[pid])
    return live


def terminal_pane(terminal, labels, claude, resumed, names):
    """A terminal as a live index pane; resumed maps the catalog's session ids among its words to their agent."""
    foreground = terminal.get("foreground") or {}
    report = reported_status(terminal)
    status = REPORTED_STATES.get(report.get("state"), report.get("state"))
    if status not in STATUS_WEIGHT:
        status = CLAUDE_STATUS.get((claude or {}).get("status"), "unknown")
    if claude:
        session_id, agent = claude["sessionId"], "claude"
    else:
        session_id = next((word for word in command_words(terminal) if word in resumed), "")
        agent = (
            resumed.get(session_id)
            or names.get((report.get("app") or "").lower())
            or program_agent(running_argv(terminal), names)
            or names.get(program_name(foreground.get("name") or ""))
            or report.get("app")
            or ""
        )
    address = terminal["address"]
    return {
        "workspace_id": labels.get("herdr.space", ""),
        "tab_id": labels.get("herdr.tab", ""),
        "pane_id": address,
        "terminal_id": address,
        "label": terminal.get("name") or "",
        "terminal_title_stripped": TITLE_MARKS.sub("", report.get("title") or terminal.get("title") or "").strip(),
        "agent": agent,
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


def index_core(session_agents=None, launchers=()) -> int:
    """Index the terminals the Herdr app shows here; session_agents names the agent of any session id in their commands."""
    try:
        terminals = core_terminals()
    except CoreError:
        # A core that is gone took its terminals with it.
        store_core([], [], {})
        raise
    by_address = {terminal["address"]: terminal for terminal in terminals}
    sessions_by_pid = live_claude_sessions()
    resumed = session_agents([word for terminal in terminals for word in command_words(terminal)]) if session_agents else {}
    names = agent_names(launchers)
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
        foreground = terminal.get("foreground") or {}
        claude = sessions_by_pid.get(foreground.get("pid")) or sessions_by_pid.get(terminal.get("pid"))
        panes.append(terminal_pane(terminal, labels, claude, resumed, names))
    return store_core(list(spaces.values()), panes, screens)
