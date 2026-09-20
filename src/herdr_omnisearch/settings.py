import configparser
import hashlib
import os
import re
import shlex
import shutil
import sqlite3
import sys
from pathlib import Path

from .herdr_socket import resolve_socket_path

DEFAULT_LINES = 500


DEFAULT_LIMIT = 30


CONFIG_CACHE = None


def split_config_list(value: str):
    if not value:
        return []
    parts = []
    for line in value.splitlines():
        for item in line.split(","):
            item = item.strip()
            if item:
                parts.append(item)
    return parts


def config_dir() -> Path:
    plugin_dir = os.environ.get("HERDR_PLUGIN_CONFIG_DIR")
    if plugin_dir:
        return Path(plugin_dir)
    installed = installed_plugin_config_dir()
    if installed.is_dir():
        return installed
    return legacy_config_dir()


def xdg_config_root() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return Path(base) if base else Path.home() / ".config"


def installed_plugin_config_dir() -> Path:
    return xdg_config_root() / "herdr" / "plugins" / "config" / "herdr.omnisearch"


def legacy_config_dir() -> Path:
    return xdg_config_root() / "herdr-omnisearch"


def default_config():
    return {
        "herdr_bin": "herdr",
        "fallback_cwd": str(Path.home()),
        "archive_enabled": False,
        "archive_window_days": 14,
        "archive_agents": ["codex", "claude"],
        "archive": {
            "codex": {
                "sessions": [str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl")],
                "thread_names": str(Path.home() / ".codex" / "session_index.jsonl"),
                "resume": "codex resume -C {cwd} {session_id}",
                "launcher": "agent",
                "kind": "codex",
                "start_timeout_ms": 60000,
            },
            "claude": {
                "sessions": [str(Path.home() / ".claude" / "projects" / "*" / "*.jsonl")],
                "resume": "claude --resume {session_id}",
                "launcher": "agent",
                "kind": "claude",
                "start_timeout_ms": 60000,
            },
        },
        "skip_label_contains": ["omnisearch"],
        "skip_workspace_cwd_pairs": [],
        "skip_unknown_without_agent": True,
        "strip_prefixes": [],
        "worktree_markers": ["worktrees"],
        "remove_words": [],
        "exact_workspace_labels": {},
    }


def app_config():
    global CONFIG_CACHE
    if CONFIG_CACHE is not None:
        return CONFIG_CACHE

    cfg = default_config()
    parser = configparser.ConfigParser()
    parser.optionxform = str
    paths = []
    override = os.environ.get("HERDR_OMNISEARCH_CONFIG")
    if override:
        paths.append(Path(override).expanduser())
    else:
        legacy = legacy_config_dir() / "config.ini"
        plugin = config_dir() / "config.ini"
        paths.append(legacy)
        if plugin != legacy:
            paths.append(plugin)
    parser.read([str(path) for path in paths if path.exists()])

    if parser.has_section("herdr"):
        cfg["herdr_bin"] = parser.get("herdr", "bin", fallback=cfg["herdr_bin"])
        cfg["fallback_cwd"] = parser.get("herdr", "fallback_cwd", fallback=cfg["fallback_cwd"])

    if parser.has_section("archive"):
        cfg["archive_enabled"] = parser.getboolean(
            "archive", "enabled", fallback=cfg["archive_enabled"]
        )
        cfg["archive_window_days"] = max(
            1, parser.getint("archive", "window_days", fallback=cfg["archive_window_days"])
        )
        agents = split_config_list(parser.get("archive", "agents", fallback=""))
        if agents:
            cfg["archive_agents"] = agents

    for agent in list(cfg["archive"]):
        section = f"archive.{agent}"
        if not parser.has_section(section):
            continue
        sessions = split_config_list(parser.get(section, "sessions", fallback=""))
        if sessions:
            cfg["archive"][agent]["sessions"] = sessions
        if parser.has_option(section, "thread_names"):
            cfg["archive"][agent]["thread_names"] = parser.get(section, "thread_names")
        if parser.has_option(section, "resume"):
            cfg["archive"][agent]["resume"] = parser.get(section, "resume")
        if parser.has_option(section, "launcher"):
            cfg["archive"][agent]["launcher"] = parser.get(section, "launcher").strip().lower()
        if parser.has_option(section, "kind"):
            cfg["archive"][agent]["kind"] = parser.get(section, "kind").strip().lower()
        if parser.has_option(section, "start_timeout_ms"):
            cfg["archive"][agent]["start_timeout_ms"] = max(
                1000, parser.getint(section, "start_timeout_ms")
            )

    for section in parser.sections():
        if section.startswith("archive.") and section.split(".", 1)[1] not in cfg["archive"]:
            agent = section.split(".", 1)[1]
            cfg["archive"][agent] = {
                "sessions": split_config_list(parser.get(section, "sessions", fallback="")),
                "resume": parser.get(section, "resume", fallback=""),
                "launcher": parser.get(section, "launcher", fallback="agent").strip().lower(),
                "kind": parser.get(section, "kind", fallback=agent).strip().lower(),
                "start_timeout_ms": max(
                    1000, parser.getint(section, "start_timeout_ms", fallback=60000)
                ),
            }
            if parser.has_option(section, "thread_names"):
                cfg["archive"][agent]["thread_names"] = parser.get(section, "thread_names")

    if parser.has_section("skip"):
        labels = split_config_list(parser.get("skip", "pane_label_contains", fallback=""))
        if labels:
            cfg["skip_label_contains"] = labels
        pairs = split_config_list(parser.get("skip", "workspace_cwd_pairs", fallback=""))
        cfg["skip_workspace_cwd_pairs"] = [
            tuple(part.strip() for part in pair.split("|", 1))
            for pair in pairs
            if "|" in pair
        ]
        cfg["skip_unknown_without_agent"] = parser.getboolean(
            "skip",
            "unknown_without_agent",
            fallback=cfg["skip_unknown_without_agent"],
        )

    if parser.has_section("workspace_labels"):
        prefixes = split_config_list(parser.get("workspace_labels", "strip_prefixes", fallback=""))
        markers = split_config_list(parser.get("workspace_labels", "worktree_markers", fallback=""))
        remove_words = split_config_list(parser.get("workspace_labels", "remove_words", fallback=""))
        if prefixes:
            cfg["strip_prefixes"] = prefixes
        if markers:
            cfg["worktree_markers"] = markers
        if remove_words:
            cfg["remove_words"] = remove_words

    if parser.has_section("workspace_labels.exact"):
        cfg["exact_workspace_labels"] = dict(parser.items("workspace_labels.exact"))

    CONFIG_CACHE = cfg
    return cfg


def data_dir() -> Path:
    plugin_dir = os.environ.get("HERDR_PLUGIN_STATE_DIR")
    if plugin_dir:
        path = Path(plugin_dir)
    else:
        installed = installed_plugin_state_dir()
        path = installed if installed.is_dir() else legacy_data_dir()
    return ensure_private_directory(path, "plugin state")


def ensure_private_directory(
    path: Path,
    purpose: str,
    *,
    repair_existing_permissions: bool = True,
) -> Path:
    path = path.expanduser()
    existed = path.exists()
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise sqlite3.OperationalError(
            f"cannot create {purpose} directory {path}: {exc}"
        ) from exc
    if not path.is_dir():
        raise sqlite3.OperationalError(
            f"{purpose} path is not a directory: {path}"
        )
    if repair_existing_permissions or not existed:
        try:
            path.chmod(0o700)
        except OSError as exc:
            if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                raise sqlite3.OperationalError(
                    f"cannot make {purpose} directory private and writable {path}: {exc}"
                ) from exc
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        raise sqlite3.OperationalError(
            f"{purpose} directory is not readable, writable, and searchable: {path}"
        )
    return path


def installed_plugin_state_dir() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else Path.home() / ".local" / "state"
    return root / "herdr" / "plugins" / "herdr.omnisearch"


def legacy_data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    if base:
        return Path(base) / "herdr-omnisearch"
    return Path.home() / ".local" / "share" / "herdr-omnisearch"


def cli_command():
    override = os.environ.get("HERDR_OMNISEARCH_COMMAND")
    if override:
        return shlex.split(override)
    plugin_root = os.environ.get("HERDR_PLUGIN_ROOT")
    if plugin_root:
        plugin_command = Path(plugin_root) / "bin" / "herdr-omnisearch"
        if plugin_command.is_file() and os.access(plugin_command, os.X_OK):
            return [str(plugin_command)]
    command = shutil.which("herdr-omnisearch")
    if command:
        return [command]
    argv0 = Path(sys.argv[0])
    if argv0.exists() and os.access(argv0, os.X_OK):
        return [str(argv0)]
    return [sys.executable, "-m", "herdr_omnisearch"]


def cli_command_string() -> str:
    return " ".join(shlex.quote(part) for part in cli_command())


def herdr_bin() -> str:
    return os.environ.get("HERDR_BIN_PATH") or os.environ.get("HERDR_BIN") or app_config()["herdr_bin"]


def herdr_session_identity():
    """Return (session_key, socket_path) for the Herdr session this process targets.

    The key stays stable per socket so concurrent sessions on one machine own
    disjoint index rows, watcher locks, and background-index locks.
    """
    socket_path = resolve_socket_path()
    name = os.environ.get("HERDR_SESSION") or ""
    if not name:
        parent = os.path.dirname(socket_path)
        if os.path.basename(os.path.dirname(parent)) == "sessions":
            name = os.path.basename(parent)
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", name).strip("-") or "default"
    digest = hashlib.sha1(socket_path.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{name}-{digest}", socket_path


def herdr_session_key() -> str:
    return herdr_session_identity()[0]
