import json
import re
import shlex
import subprocess
from pathlib import Path

from .herdr_programs import AGENT_PROGRAMS, INTERPRETERS, KNOWN_PROGRAMS, SHELLS
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


def resume_commands():
    """Each catalogued agent's resume command as argv; a malformed one is left out."""
    config = app_config()
    commands = []
    for agent in config["archive_agents"]:
        try:
            commands.append((agent, shlex.split(config["archive"].get(agent, {}).get("resume") or "")))
        except ValueError:
            continue
    return commands


def agent_names(launchers=()):
    """Agents by program name: the ones OmniSearch catalogs, their resume commands', and the app's launchers."""
    names = {agent.lower(): agent for agent in [*app_config()["archive_agents"], *(agent for agent, _argv in launchers)]}
    for agent, argv in [*resume_commands(), *launchers]:
        program = launched_program(argv)
        # `gh copilot` must not make every `gh` an agent: only agents and programs Herdr doesn't know.
        if program and (program in AGENT_PROGRAMS or program not in KNOWN_PROGRAMS):
            names.setdefault(program, agent)
    return names


def launched_program(argv) -> str:
    """The program a launch command starts directly; none when a shell or an interpreter starts it."""
    # Reading further into a command line guesses: an option, a path or a script argument can look like a program.
    stem = program_stem(argv[0]) if argv and not argv[0].startswith("-") else ""
    return "" if stem in SHELLS or stem in INTERPRETERS else stem


def program_name(word: str) -> str:
    # A login shell's argv[0] is `-zsh`.
    return Path(word).name.lstrip("-").lower()


def program_stem(word: str) -> str:
    name = program_name(word)
    return next((name[: -len(ext)] for ext in SCRIPT_EXTENSIONS if name.endswith(ext)), name)


def unwrap_launch(argv):
    """The command inside the app's `sh -c '"$@"; exec "$0" -i' sh <command>` wrapper, or argv itself."""
    while (
        len(argv) > 4 and program_name(argv[0]) in SHELLS and argv[1].startswith("-") and argv[1].endswith("c")
        and ('"$@"' in argv[2] or argv[2].startswith(FISH_LAUNCH))
    ):
        argv = argv[4:]
    return argv


def running_program(argv, names) -> str:
    """The program a command runs, read as Herdr's program table reads it (herdr-app src/programs.rs)."""
    argv = unwrap_launch(list(argv))
    if not argv:
        return ""
    first = program_stem(argv[0])
    if first in INTERPRETERS:
        # The first program the interpreter runs that is known: an agent, or something else.
        for word in argv[1:]:
            stem = program_stem(word)
            if not word.startswith("-") and (stem in names or stem in KNOWN_PROGRAMS):
                return stem
    return first


def may_run_agent(program: str, names, launch_programs) -> bool:
    """False for a shell or a program Herdr knows as something other than an agent, unless agents are launched with it."""
    if program in names or program in AGENT_PROGRAMS or program in launch_programs:
        return True
    return not (program in SHELLS or program in KNOWN_PROGRAMS)


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


def terminal_pane(terminal, labels, claude, resumed, names, launch_programs=()):
    """A terminal as a live index pane; resumed maps the catalog's session ids among its words to their agent."""
    foreground = terminal.get("foreground") or {}
    report = reported_status(terminal)
    status = REPORTED_STATES.get(report.get("state"), report.get("state"))
    if status not in STATUS_WEIGHT:
        status = CLAUDE_STATUS.get((claude or {}).get("status"), "unknown")
    if claude:
        session_id, agent = claude["sessionId"], "claude"
    else:
        program = running_program(running_argv(terminal), names)
        # `rg <session id> agent.log` names a session without running it.
        words = command_words(terminal) if may_run_agent(program, names, launch_programs) else []
        session_id = next((word for word in words if word in resumed), "")
        agent = (
            resumed.get(session_id)
            or names.get((report.get("app") or "").lower())
            or names.get(program, "")
            or names.get(program_stem(foreground.get("name") or ""))
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
    # `docker run … codex resume <id>` runs a session through a program Herdr knows as something else.
    launch_programs = {program_stem(argv[0]) for _agent, argv in [*resume_commands(), *launchers] if argv} - SHELLS
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
        # Herdr writes "<space> / <tab>"; a space name may hold " / " too.
        space_name = (labels.get("where") or "").rsplit(" / ", 1)[0]
        spaces.setdefault(labels["herdr.space"], {
            "workspace_id": labels["herdr.space"],
            "label": space_name or labels["herdr.space"],
            "active_tab_id": labels.get("herdr.tab", ""),
        })
        foreground = terminal.get("foreground") or {}
        claude = sessions_by_pid.get(foreground.get("pid")) or sessions_by_pid.get(terminal.get("pid"))
        panes.append(terminal_pane(terminal, labels, claude, resumed, names, launch_programs))
    return store_core(list(spaces.values()), panes, screens)
