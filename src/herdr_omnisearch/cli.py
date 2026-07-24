#!/usr/bin/env python3
import argparse
import base64
import configparser
import curses
import errno
import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .herdr_cli import HerdrCLI, HerdrCLIError
from .herdr_socket import (
    HerdrClient,
    HerdrError,
    HerdrTimeout,
    resolve_socket_path,
    socket_is_alive,
)


DEFAULT_LINES = 500
DEFAULT_LIMIT = 30
STATUS_WEIGHT = {
    "workspace": 0,
    "working": 0,
    "blocked": 1,
    "idle": 2,
    "done": 3,
    "unknown": 4,
}
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/@+-]*")
TOKEN_TABLES = {
    "live": ("terms", "token_docs", "token_trigrams"),
    "archive": ("archive_terms", "archive_token_docs", "archive_token_trigrams"),
}
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
        "archive_max_files": 500,
        "archive_since_days": 90,
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
        cfg["archive_max_files"] = max(
            0, parser.getint("archive", "max_files", fallback=cfg["archive_max_files"])
        )
        cfg["archive_since_days"] = max(
            0, parser.getint("archive", "since_days", fallback=cfg["archive_since_days"])
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


def db_path() -> Path:
    override = os.environ.get("HERDR_OMNISEARCH_DB")
    if override:
        return Path(override)
    path = data_dir() / "index.sqlite3"
    legacy = legacy_data_dir() / "index.sqlite3"
    if needs_database_repair(path, legacy):
        # Startup, event hooks, and panes can all hit the first-start
        # migration at once; only the lock holder may move files around.
        lock_fd = os.open(path.parent / "migrate.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            repair_database_path(path)
            if path != legacy and legacy.exists() and not legacy.is_symlink():
                migrate_legacy_db(legacy, path)
        finally:
            os.close(lock_fd)
    return path


def needs_database_repair(path: Path, legacy: Path) -> bool:
    if path.is_symlink() and not path.exists():
        return True
    return path != legacy and legacy.exists() and not legacy.is_symlink()


def repair_database_path(path: Path) -> None:
    # A dangling or self-referential symlink here is debris from an
    # interrupted legacy migration; is_symlink + not exists covers both.
    if path.is_symlink() and not path.exists():
        path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.is_symlink() and not sidecar.exists():
                sidecar.unlink()
    if path.exists():
        return
    backups = sorted(path.parent.glob(path.name + ".failed-*"), reverse=True)
    for backup in backups:
        if backup.is_file() and database_has_index_data(backup):
            os.replace(backup, path)
            return


def prepare_database_path(path: Path) -> Path:
    path = path.expanduser()
    ensure_private_directory(
        path.parent,
        "database parent",
        repair_existing_permissions=False,
    )

    if path.is_symlink() and not path.exists():
        raise sqlite3.OperationalError(f"database path is a broken symlink: {path}")
    if path.exists() and not path.is_file():
        raise sqlite3.OperationalError(f"database path is not a regular file: {path}")

    if not path.exists():
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        except FileExistsError:
            # Another event hook may have won the first-start creation race.
            pass
        except OSError as exc:
            raise sqlite3.OperationalError(
                f"cannot create database file {path}: {exc}"
            ) from exc
        else:
            os.close(fd)

    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
    ):
        if candidate.is_symlink() and not candidate.exists():
            raise sqlite3.OperationalError(
                f"database sidecar path is a broken symlink: {candidate}"
            )
        if not candidate.exists():
            continue
        if not candidate.is_file():
            # WAL sidecars appear and vanish while concurrent processes
            # checkpoint; only a path that still exists as something other
            # than a regular file is an error.
            if candidate.exists():
                raise sqlite3.OperationalError(
                    f"database path is not a regular file: {candidate}"
                )
            continue
        try:
            candidate.chmod(0o600)
        except FileNotFoundError:
            continue
        except OSError as exc:
            if not os.access(candidate, os.R_OK | os.W_OK):
                raise sqlite3.OperationalError(
                    f"cannot make database file private and writable {candidate}: {exc}"
                ) from exc

    if not os.access(path, os.R_OK | os.W_OK):
        raise sqlite3.OperationalError(
            f"database file is not readable and writable: {path}"
        )
    return path


def database_has_index_data(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            for table in ("docs", "archive_sessions"):
                if table in tables and conn.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone():
                    return True
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    return False


def migrate_legacy_db(legacy: Path, path: Path) -> None:
    if legacy.is_symlink() or not legacy.is_file():
        # Another process already migrated (or the source vanished); moving a
        # symlink over the real database would corrupt it.
        return
    if path.exists() and database_has_index_data(path):
        return
    if path.exists():
        failed = path.with_name(f"{path.name}.failed-{int(time.time())}")
        os.replace(path, failed)

    # Checkpoint first so the database can be moved as one durable file.
    conn = sqlite3.connect(legacy)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()

    try:
        os.replace(legacy, path)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
        source = sqlite3.connect(f"file:{legacy}?mode=ro", uri=True)
        destination = sqlite3.connect(path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        legacy.unlink()

    legacy.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy.symlink_to(path)
    except FileExistsError:
        pass
    for suffix in ("-wal", "-shm"):
        old = Path(str(legacy) + suffix)
        new = Path(str(path) + suffix)
        if old.exists() and not old.is_symlink():
            os.replace(old, new)
        if not old.exists() and not old.is_symlink():
            old.symlink_to(new)


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


def connect():
    path = prepare_database_path(db_path())
    conn = None
    try:
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        init_schema(conn)
        return conn
    except sqlite3.Error as exc:
        if conn is not None:
            conn.close()
        raise sqlite3.OperationalError(
            f"cannot initialize database {path}: {exc}"
        ) from exc


def init_schema(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS docs (
            stable_id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            workspace_label TEXT,
            tab_id TEXT NOT NULL,
            pane_id TEXT NOT NULL,
            terminal_id TEXT,
            pane_label TEXT,
            agent TEXT,
            agent_session_id TEXT,
            agent_status TEXT,
            cwd TEXT,
            foreground_cwd TEXT,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            indexed_at INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            stable_id UNINDEXED,
            body,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS terms (
            token TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS token_docs (
            token TEXT NOT NULL,
            stable_id TEXT NOT NULL,
            PRIMARY KEY (token, stable_id)
        );

        CREATE TABLE IF NOT EXISTS token_trigrams (
            trigram TEXT NOT NULL,
            token TEXT NOT NULL,
            PRIMARY KEY (trigram, token)
        );

        CREATE INDEX IF NOT EXISTS idx_token_docs_stable_id ON token_docs(stable_id);
        CREATE INDEX IF NOT EXISTS idx_token_trigrams_token ON token_trigrams(token);

        CREATE TABLE IF NOT EXISTS archive_sessions (
            session_key TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            session_id TEXT NOT NULL,
            space_label TEXT,
            title TEXT,
            cwd TEXT,
            path TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT,
            indexed_at INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS archive_docs (
            stable_id TEXT PRIMARY KEY,
            session_key TEXT NOT NULL,
            agent TEXT NOT NULL,
            session_id TEXT NOT NULL,
            space_label TEXT,
            title TEXT,
            cwd TEXT,
            path TEXT NOT NULL,
            started_at TEXT,
            updated_at TEXT,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            indexed_at INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS archive_docs_fts USING fts5(
            stable_id UNINDEXED,
            body,
            tokenize = 'porter unicode61'
        );

        CREATE TABLE IF NOT EXISTS archive_terms (
            token TEXT PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS archive_token_docs (
            token TEXT NOT NULL,
            stable_id TEXT NOT NULL,
            PRIMARY KEY (token, stable_id)
        );

        CREATE TABLE IF NOT EXISTS archive_token_trigrams (
            trigram TEXT NOT NULL,
            token TEXT NOT NULL,
            PRIMARY KEY (trigram, token)
        );

        CREATE INDEX IF NOT EXISTS idx_archive_docs_session_key ON archive_docs(session_key);
        CREATE INDEX IF NOT EXISTS idx_archive_docs_agent ON archive_docs(agent);
        CREATE INDEX IF NOT EXISTS idx_archive_token_docs_stable_id ON archive_token_docs(stable_id);
        CREATE INDEX IF NOT EXISTS idx_archive_token_trigrams_token ON archive_token_trigrams(token);
        """
    )
    ensure_column(conn, "docs", "agent_session_id", "TEXT")
    ensure_column(conn, "docs", "herdr_session", "TEXT")
    ensure_column(conn, "docs", "socket_path", "TEXT")
    ensure_column(conn, "archive_sessions", "space_label", "TEXT")
    ensure_column(conn, "archive_docs", "space_label", "TEXT")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_docs_agent_session_id ON docs(agent_session_id);
        CREATE INDEX IF NOT EXISTS idx_docs_herdr_session ON docs(herdr_session);
        CREATE INDEX IF NOT EXISTS idx_archive_sessions_space_label ON archive_sessions(space_label);
        CREATE INDEX IF NOT EXISTS idx_archive_docs_space_label ON archive_docs(space_label);
        """
    )


def ensure_column(conn, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def clean_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", value)
    value = "".join(ch for ch in value if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    return value.strip()


def tokens(value: str):
    out = []
    for token in TOKEN_RE.findall(value or ""):
        token = token.lower().strip("._:/@+-")
        if 2 <= len(token) <= 64:
            out.append(token)
    return out


def token_trigrams(token: str):
    if len(token) <= 3:
        return {token}
    return {token[i : i + 3] for i in range(len(token) - 2)}


def prefix_upper_bound(prefix: str):
    if not prefix:
        return None
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


def edit_distance_limited(left: str, right: str, limit: int):
    if abs(len(left) - len(right)) > limit:
        return None
    if left == right:
        return 0
    if not left:
        return len(right) if len(right) <= limit else None
    if not right:
        return len(left) if len(left) <= limit else None

    previous = list(range(len(right) + 1))
    for i, left_ch in enumerate(left, 1):
        current = [i]
        row_min = current[0]
        for j, right_ch in enumerate(right, 1):
            cost = 0 if left_ch == right_ch else 1
            value = min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + cost,
            )
            current.append(value)
            if value < row_min:
                row_min = value
        if row_min > limit:
            return None
        previous = current
    distance = previous[-1]
    return distance if distance <= limit else None


def fuzzy_distance_limit(term: str) -> int:
    length = len(term)
    if length <= 3:
        return 0
    if length <= 4:
        return 1
    if length <= 7:
        return 2
    if length <= 12:
        return 3
    return 4


def fuzzy_score_threshold(term: str) -> float:
    length = len(term)
    if length <= 4:
        return 0.78
    if length <= 7:
        return 0.75
    if length <= 12:
        return 0.68
    return 0.66


def score_token_candidate(term: str, token: str) -> float:
    if term == token:
        return 1.0
    if token.startswith(term):
        return 0.96
    if len(term) >= 3 and term.startswith(token):
        return 0.88
    if len(term) < 4:
        return 0.0

    max_distance = fuzzy_distance_limit(term)
    distance = edit_distance_limited(term, token, max_distance)
    if distance is None:
        return 0.0
    return max(0.0, 1.0 - (distance / (max(len(term), len(token)) + 1)))


def token_tables(scope: str):
    try:
        return TOKEN_TABLES[scope]
    except KeyError as exc:
        raise ValueError(f"unknown token scope: {scope}") from exc


def candidate_tokens_for_term(conn, term: str, *, limit: int = 180, scope: str = "live"):
    term = term.lower()
    candidates = set()
    terms_table, _token_docs_table, trigrams_table = token_tables(scope)

    upper = prefix_upper_bound(term)
    if upper:
        rows = conn.execute(
            f"""
            SELECT token
            FROM {terms_table}
            WHERE token = ?
               OR (token >= ? AND token < ?)
            ORDER BY length(token) ASC, token ASC
            LIMIT ?
            """,
            (term, term, upper, limit),
        ).fetchall()
        candidates.update(row["token"] for row in rows)

    if len(term) >= 4:
        grams = sorted(token_trigrams(term))
        overlap_floor = max(1, min(len(grams), len(grams) - fuzzy_distance_limit(term)))
        placeholders = ",".join("?" for _ in grams)
        rows = conn.execute(
            f"""
            SELECT token, COUNT(*) AS overlap
            FROM {trigrams_table}
            WHERE trigram IN ({placeholders})
            GROUP BY token
            HAVING overlap >= ?
            ORDER BY overlap DESC, abs(length(token) - ?) ASC, length(token) ASC
            LIMIT ?
            """,
            (*grams, overlap_floor, len(term), limit * 2),
        ).fetchall()
        candidates.update(row["token"] for row in rows)

    scored = []
    threshold = fuzzy_score_threshold(term)
    for token in candidates:
        score = score_token_candidate(term, token)
        if score >= threshold:
            scored.append((token, score))
    scored.sort(key=lambda item: (-item[1], abs(len(item[0]) - len(term)), item[0]))
    return scored[:limit]


def has_prefix_token(conn, term: str, *, scope: str = "live") -> bool:
    upper = prefix_upper_bound(term)
    if not upper:
        return False
    terms_table, _token_docs_table, _trigrams_table = token_tables(scope)
    return (
        conn.execute(
            f"""
            SELECT 1
            FROM {terms_table}
            WHERE token >= ? AND token < ?
            LIMIT 1
            """,
            (term, upper),
        ).fetchone()
        is not None
    )


def label_match_score(label: str, query_terms) -> int:
    label_terms = set(tokens(label or ""))
    if not label_terms or not query_terms:
        return 0
    score = 0
    for term in query_terms:
        if any(score_token_candidate(term, label_term) >= fuzzy_score_threshold(term) for label_term in label_terms):
            score += 1
    return score


def mark_space_matches(rows, query: str):
    query_terms = list(dict.fromkeys(tokens(query)))
    if not query_terms:
        return
    for row in rows:
        label = row.get("workspace_label") or row.get("space_label") or ""
        score = label_match_score(label, query_terms)
        if score:
            row["_space_match_score"] = score


def space_sort_weight(row) -> int:
    return -int(row.get("_space_match_score") or 0)


def chunk_text(text: str, *, max_lines: int = 55, overlap: int = 6):
    lines = [line.rstrip() for line in clean_text(text).splitlines()]
    lines = [line for line in lines if line.strip()]
    if not lines:
        return []
    chunks = []
    step = max(1, max_lines - overlap)
    for start in range(0, len(lines), step):
        piece = lines[start : start + max_lines]
        if piece:
            chunks.append("\n".join(piece))
        if start + max_lines >= len(lines):
            break
    return chunks


def pane_agent_session_id(pane) -> str:
    session = pane.get("agent_session") or {}
    if isinstance(session, dict):
        return session.get("value") or ""
    return ""


def pane_agent(pane) -> str:
    session = pane.get("agent_session") or {}
    if isinstance(session, dict) and session.get("agent"):
        return session["agent"]
    return pane.get("agent") or ""


def merge_agent_records(panes, agents):
    agents_by_pane = {
        agent.get("pane_id"): agent
        for agent in agents
        if isinstance(agent, dict) and agent.get("pane_id")
    }
    merged = []
    for pane in panes:
        record = dict(pane)
        live_agent = agents_by_pane.get(pane.get("pane_id"))
        if live_agent:
            record.update(live_agent)
        record["agent"] = pane_agent(record)
        merged.append(record)
    return merged


def metadata_text(pane, workspace_label: str) -> str:
    agent_session_id = pane_agent_session_id(pane)
    fields = [
        f"workspace {workspace_label}",
        f"workspace_id {pane.get('workspace_id', '')}",
        f"tab_id {pane.get('tab_id', '')}",
        f"pane_id {pane.get('pane_id', '')}",
        f"terminal_id {pane.get('terminal_id', '')}",
        f"label {pane.get('label', '')}",
        f"agent {pane_agent(pane)}",
        f"agent_session {agent_session_id}",
        f"status {pane.get('agent_status', '')}",
        f"cwd {pane.get('cwd', '')}",
        f"foreground_cwd {pane.get('foreground_cwd', '')}",
    ]
    return "\n".join(field for field in fields if field.strip())


def workspace_metadata_text(workspace) -> str:
    fields = [
        "type workspace",
        f"workspace {workspace.get('label') or workspace.get('workspace_id', '')}",
        f"workspace_id {workspace.get('workspace_id', '')}",
        f"tab_id {workspace.get('active_tab_id', '')}",
        f"pane_count {workspace.get('pane_count', '')}",
        f"tab_count {workspace.get('tab_count', '')}",
    ]
    return "\n".join(field for field in fields if field.strip())


def pane_recent_text(client: HerdrClient, agent_cli: HerdrCLI, pane, lines: int) -> str:
    pane_id = pane.get("pane_id") or ""
    if pane_id and pane_agent(pane):
        try:
            return agent_cli.agent_read(pane_id, lines)
        except HerdrCLIError:
            pass
    try:
        return client.pane_read(pane_id, lines)
    except HerdrError as exc:
        return f"[pane read failed] {exc}"


def should_skip_pane(pane, workspace_label: str, *, include_wrappers: bool) -> bool:
    if include_wrappers:
        return False
    cfg = app_config()
    label = (pane.get("label") or "").lower()
    workspace = (workspace_label or "").lower()
    cwd = (pane.get("cwd") or "").lower()
    status = pane.get("agent_status") or "unknown"
    agent = pane.get("agent") or ""
    if any(needle.lower() in label for needle in cfg["skip_label_contains"]):
        return True
    if any(workspace == pair[0].lower() and cwd == pair[1].lower() for pair in cfg["skip_workspace_cwd_pairs"]):
        return True
    if cfg["skip_unknown_without_agent"] and status == "unknown" and not agent:
        return True
    return False


def index_session(lines: int, include_empty: bool, include_wrappers: bool, snapshot=None) -> int:
    client = HerdrClient()
    agent_cli = HerdrCLI(herdr_bin())
    snapshot = snapshot or client.snapshot()
    workspaces_payload = snapshot.get("workspaces", [])
    workspace_labels = {
        workspace["workspace_id"]: workspace.get("label") or workspace["workspace_id"]
        for workspace in workspaces_payload
    }
    panes = merge_agent_records(snapshot.get("panes", []), agent_cli.agent_list())

    session_key, session_socket = herdr_session_identity()
    now = int(time.time())
    docs = []
    doc_tokens = []
    indexable_panes = []
    indexable_workspace_counts = {}
    for pane in panes:
        workspace_label = workspace_labels.get(pane.get("workspace_id"), pane.get("workspace_id", ""))
        if should_skip_pane(pane, workspace_label, include_wrappers=include_wrappers):
            continue
        indexable_panes.append((pane, workspace_label))
        workspace_id = pane.get("workspace_id", "")
        indexable_workspace_counts[workspace_id] = indexable_workspace_counts.get(workspace_id, 0) + 1

    for workspace in workspaces_payload:
        workspace_id = workspace.get("workspace_id", "")
        if not include_wrappers and indexable_workspace_counts.get(workspace_id, 0) == 0:
            continue
        workspace_label = workspace.get("label") or workspace_id
        body = workspace_metadata_text(workspace)
        digest = hashlib.sha1(
            f"{session_key}\0workspace\0{workspace_id}\0{body}".encode("utf-8", "replace")
        ).hexdigest()
        docs.append(
            {
                "stable_id": digest,
                "herdr_session": session_key,
                "socket_path": session_socket,
                "workspace_id": workspace_id,
                "workspace_label": workspace_label,
                "tab_id": workspace.get("active_tab_id", ""),
                "pane_id": f"workspace:{workspace_id}",
                "terminal_id": "",
                "pane_label": workspace_label,
                "agent": "",
                "agent_session_id": "",
                "agent_status": "workspace",
                "cwd": "",
                "foreground_cwd": "",
                "chunk_index": 0,
                "content": body,
                "body": body,
                "indexed_at": now,
            }
        )
        for token in set(tokens(body)):
            doc_tokens.append((token, digest))

    for pane, workspace_label in indexable_panes:
        pane_id = pane["pane_id"]
        agent_session_id = pane_agent_session_id(pane)
        meta = metadata_text(pane, workspace_label)
        recent = pane_recent_text(client, agent_cli, pane, lines)
        chunks = chunk_text(recent)
        if include_empty or not chunks:
            chunks = chunks or ["[empty pane]\n" + meta]
        for idx, chunk in enumerate(chunks):
            body = f"{meta}\n\n{chunk}"
            digest = hashlib.sha1(
                f"{session_key}\0{pane_id}\0{idx}\0{body}".encode("utf-8", "replace")
            ).hexdigest()
            docs.append(
                {
                    "stable_id": digest,
                    "herdr_session": session_key,
                    "socket_path": session_socket,
                    "workspace_id": pane.get("workspace_id", ""),
                    "workspace_label": workspace_label,
                    "tab_id": pane.get("tab_id", ""),
                    "pane_id": pane_id,
                    "terminal_id": pane.get("terminal_id", ""),
                    "pane_label": pane.get("label", ""),
                    "agent": pane_agent(pane),
                    "agent_session_id": agent_session_id,
                    "agent_status": pane.get("agent_status", ""),
                    "cwd": pane.get("cwd", ""),
                    "foreground_cwd": pane.get("foreground_cwd", ""),
                    "chunk_index": idx,
                    "content": chunk,
                    "body": body,
                    "indexed_at": now,
                }
            )
            indexed_tokens = set(tokens(body))
            for token in indexed_tokens:
                doc_tokens.append((token, digest))

    conn = connect()
    with conn:
        # Replace only this session's rows so concurrent Herdr sessions on the
        # same machine never clobber each other. Rows without a session are
        # pre-upgrade leftovers and are swept by whichever session runs first.
        stale = (
            "SELECT stable_id FROM docs WHERE herdr_session = :session OR herdr_session IS NULL"
        )
        conn.execute(
            f"DELETE FROM docs_fts WHERE stable_id IN ({stale})", {"session": session_key}
        )
        conn.execute(
            f"DELETE FROM token_docs WHERE stable_id IN ({stale})", {"session": session_key}
        )
        conn.execute(
            "DELETE FROM docs WHERE herdr_session = :session OR herdr_session IS NULL",
            {"session": session_key},
        )
        conn.executemany(
            """
            INSERT INTO docs (
                stable_id, herdr_session, socket_path, workspace_id, workspace_label, tab_id,
                terminal_id, pane_id, pane_label, agent, agent_session_id, agent_status, cwd,
                foreground_cwd, chunk_index, content, indexed_at
            )
            VALUES (
                :stable_id, :herdr_session, :socket_path, :workspace_id, :workspace_label, :tab_id,
                :terminal_id, :pane_id, :pane_label, :agent, :agent_session_id, :agent_status, :cwd,
                :foreground_cwd, :chunk_index, :content, :indexed_at
            )
            """,
            docs,
        )
        conn.executemany(
            "INSERT INTO docs_fts (stable_id, body) VALUES (:stable_id, :body)",
            docs,
        )
        if doc_tokens:
            unique_terms = sorted({token for token, _stable_id in doc_tokens})
            conn.executemany(
                "INSERT OR IGNORE INTO terms (token) VALUES (?)",
                [(token,) for token in unique_terms],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO token_docs (token, stable_id) VALUES (?, ?)",
                doc_tokens,
            )
            trigram_rows = []
            for token in unique_terms:
                for trigram in token_trigrams(token):
                    trigram_rows.append((trigram, token))
            conn.executemany(
                "INSERT OR IGNORE INTO token_trigrams (trigram, token) VALUES (?, ?)",
                trigram_rows,
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_indexed_at', ?)",
            (str(now),),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
            (f"last_indexed_at:{session_key}", str(now)),
        )
        reap_dead_sessions(conn, session_key)
    conn.close()
    return len(docs)


def reap_dead_sessions(conn, current_key: str) -> int:
    """Drop index rows and state files of sessions whose socket is gone."""
    rows = conn.execute(
        """
        SELECT DISTINCT herdr_session, socket_path FROM docs
        WHERE herdr_session IS NOT NULL AND herdr_session != ?
        """,
        (current_key,),
    ).fetchall()
    dead = [
        row["herdr_session"]
        for row in rows
        if not socket_is_alive(row["socket_path"] or "")
    ]
    for key in dead:
        stale = "SELECT stable_id FROM docs WHERE herdr_session = ?"
        conn.execute(f"DELETE FROM docs_fts WHERE stable_id IN ({stale})", (key,))
        conn.execute(f"DELETE FROM token_docs WHERE stable_id IN ({stale})", (key,))
        conn.execute("DELETE FROM docs WHERE herdr_session = ?", (key,))
        conn.execute("DELETE FROM meta WHERE key = ?", (f"last_indexed_at:{key}",))
        stop_watcher_at(data_dir() / f"watch-{key}.pid")
        for leftover in (
            data_dir() / f"watch-{key}.pid",
            data_dir() / f"watch-{key}.log",
            data_dir() / f"index-{key}.lock",
        ):
            if lock_is_held(leftover):
                continue
            try:
                leftover.unlink()
            except OSError:
                pass
    return len(dead)


def release_index_lock():
    lock = os.environ.get("HERDR_OMNISEARCH_LOCK")
    if not lock:
        return
    try:
        Path(lock).unlink(missing_ok=True)
    except OSError:
        pass


def try_exclusive_lock(path: Path):
    """Take a non-blocking exclusive flock on path.

    Returns an open file descriptor on success or None when another process
    holds the lock. The lock lives until every descriptor for it is closed,
    so it follows the owning process (or a child that inherits the fd) and
    can never go stale.
    """
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode("utf-8"))
    return fd


def lock_is_held(path: Path) -> bool:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError:
        return True
    finally:
        os.close(fd)
    return False


def spawn_locked_background(cmd, lock_fd) -> None:
    # The child inherits lock_fd, so the flock is held for its whole
    # lifetime and releases automatically when it exits or crashes.
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
            pass_fds=(lock_fd,),
        )
    finally:
        os.close(lock_fd)


def maybe_background_index(lines: int, include_empty: bool, include_wrappers: bool, stale_seconds: int):
    session_key = herdr_session_key()
    conn = connect()
    try:
        doc_count = conn.execute(
            "SELECT COUNT(*) FROM docs WHERE herdr_session = ?", (session_key,)
        ).fetchone()[0]
        last = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (f"last_indexed_at:{session_key}",)
        ).fetchone()
    finally:
        conn.close()

    last_indexed = int(last[0]) if last else 0
    if doc_count and int(time.time()) - last_indexed < stale_seconds:
        return

    lock_fd = try_exclusive_lock(data_dir() / f"index-{session_key}.lock")
    if lock_fd is None:
        return

    cmd = [*cli_command(), "index", "--lines", str(lines)]
    if include_empty:
        cmd.append("--include-empty")
    if include_wrappers:
        cmd.append("--include-wrappers")
    spawn_locked_background(cmd, lock_fd)


def clip_text(value: str, limit: int = 5000) -> str:
    value = clean_text(value)
    if len(value) <= limit:
        return value
    return value[:limit] + "..."


def is_archive_noise(text: str) -> bool:
    text = clean_text(text)
    if not text:
        return True
    noise_prefixes = (
        "# AGENTS.md instructions",
        "<environment_context>",
        "<permissions instructions>",
        "<collaboration_mode>",
        "<apps_instructions>",
        "<skills_instructions>",
        "<plugins_instructions>",
        "<INSTRUCTIONS>",
    )
    if any(text.startswith(prefix) for prefix in noise_prefixes):
        return True
    if len(text) > 12000 and ("You are Codex" in text or "AGENTS.md" in text):
        return True
    return False


def extract_message_text(content) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    pieces = []
    for item in content:
        if isinstance(item, str):
            pieces.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type in ("text", "input_text", "output_text"):
            pieces.append(item.get("text") or "")
    return "\n".join(piece for piece in pieces if piece)


def append_archive_part(parts, role: str, text: str):
    if is_archive_noise(text):
        return
    parts.append(f"{role}: {clip_text(text)}")


def title_from_text(text: str, fallback: str) -> str:
    text = " ".join(clean_text(text).split())
    if not text:
        return fallback
    return shorten(text, 80)


def iso_to_epoch(value: str) -> float:
    if not value:
        return 0.0
    try:
        normalized = value.replace("Z", "+00:00")
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return 0.0


def load_codex_thread_names():
    path = Path(app_config()["archive"].get("codex", {}).get("thread_names", "")).expanduser()
    names = {}
    if not path or not path.exists():
        return names
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            session_id = item.get("id")
            if session_id:
                names[session_id] = item.get("thread_name") or ""
    return names


def parse_codex_archive(path: Path, thread_names):
    session_id = ""
    cwd = ""
    started_at = ""
    updated_at = ""
    title = ""
    parts = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = item.get("timestamp") or ""
            if timestamp:
                updated_at = timestamp
                started_at = started_at or timestamp
            item_type = item.get("type")
            payload = item.get("payload") or {}
            if item_type == "session_meta":
                session_id = payload.get("id") or session_id
                cwd = payload.get("cwd") or cwd
                started_at = payload.get("timestamp") or started_at
                updated_at = payload.get("timestamp") or updated_at
                continue
            if item_type == "turn_context":
                cwd = payload.get("cwd") or cwd
                continue
            if item_type == "event_msg":
                continue
            if item_type == "response_item" and payload.get("type") == "message":
                role = payload.get("role") or "agent"
                append_archive_part(parts, role, extract_message_text(payload.get("content")))

    if not session_id:
        match = re.search(r"([0-9a-f]{8}-[0-9a-f-]{27,})", path.name)
        session_id = match.group(1) if match else hashlib.sha1(str(path).encode()).hexdigest()
    first_text = parts[0].split(":", 1)[1] if parts else ""
    title = thread_names.get(session_id) or title_from_text(first_text, session_id[:8])
    return {
        "agent": "codex",
        "session_id": session_id,
        "title": title,
        "cwd": cwd,
        "path": str(path),
        "started_at": started_at,
        "updated_at": updated_at or started_at,
        "content": "\n".join(parts),
    }


def parse_claude_archive(path: Path):
    session_id = ""
    cwd = ""
    started_at = ""
    updated_at = ""
    slug = ""
    parts = []

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            timestamp = item.get("timestamp") or ""
            if timestamp:
                updated_at = timestamp
                started_at = started_at or timestamp
            session_id = item.get("sessionId") or session_id
            cwd = item.get("cwd") or cwd
            slug = item.get("slug") or slug
            item_type = item.get("type")
            if item_type not in ("user", "assistant"):
                continue
            message = item.get("message") or {}
            role = message.get("role") or item_type
            append_archive_part(parts, role, extract_message_text(message.get("content")))

    if not session_id:
        session_id = path.stem
    first_text = parts[0].split(":", 1)[1] if parts else ""
    title = slug.replace("-", " ") if slug else title_from_text(first_text, session_id[:8])
    return {
        "agent": "claude",
        "session_id": session_id,
        "title": title,
        "cwd": cwd,
        "path": str(path),
        "started_at": started_at,
        "updated_at": updated_at or started_at,
        "content": "\n".join(parts),
    }


def archive_paths(agent: str):
    globs = app_config()["archive"].get(agent, {}).get("sessions", [])
    paths = []
    for pattern in globs:
        paths.extend(Path(path) for path in glob.glob(os.path.expanduser(pattern), recursive=True))
    return sorted(paths)


def selected_archive_sources(agents: str):
    configured = app_config()["archive_agents"]
    selected = {agent.strip() for agent in (agents or ",".join(configured)).split(",") if agent.strip()}
    return selected or set(configured)


def require_archive_enabled() -> None:
    if not app_config()["archive_enabled"]:
        raise RuntimeError(
            "archive indexing is disabled; set enabled = true in the [archive] config section"
        )


def index_archive(agents: str = "", max_files=None, since_days=None) -> tuple[int, int]:
    require_archive_enabled()
    cfg = app_config()
    if max_files is None:
        max_files = cfg["archive_max_files"]
    if since_days is None:
        since_days = cfg["archive_since_days"]
    selected = selected_archive_sources(agents)
    cutoff = time.time() - since_days * 86400 if since_days else 0
    sessions = []
    thread_names = load_codex_thread_names() if "codex" in selected else {}

    source_paths = []
    for agent in selected:
        source_paths.extend((agent, path) for path in archive_paths(agent))
    source_paths.sort(key=lambda item: item[1].stat().st_mtime if item[1].exists() else 0, reverse=True)
    if max_files:
        source_paths = source_paths[:max_files]

    for agent, path in source_paths:
        if cutoff and path.stat().st_mtime < cutoff:
            continue
        try:
            if agent == "codex":
                session = parse_codex_archive(path, thread_names)
            elif agent == "claude":
                session = parse_claude_archive(path)
            else:
                continue
        except OSError:
            continue
        if not session.get("content"):
            continue
        sessions.append(session)

    unique_sessions = {}
    for session in sessions:
        session_key = f"{session['agent']}:{session['session_id']}"
        current = unique_sessions.get(session_key)
        if current is None or iso_to_epoch(session.get("updated_at") or "") >= iso_to_epoch(current.get("updated_at") or ""):
            unique_sessions[session_key] = session
    sessions = list(unique_sessions.values())

    now = int(time.time())
    conn = connect()
    try:
        live_session_spaces = live_space_labels_by_session(conn)
        live_spaces = live_space_labels_by_cwd(conn)
    finally:
        conn.close()
    session_rows = []
    doc_rows = []
    doc_tokens = []
    for session in sessions:
        session_key = f"{session['agent']}:{session['session_id']}"
        cwd = session.get("cwd") or ""
        live_session_key = (session["agent"], session["session_id"])
        space_label = (
            live_session_spaces.get(live_session_key)
            or live_spaces.get(clean_text(cwd))
            or derive_space_label_from_cwd(cwd)
        )
        session_rows.append(
            {
                "session_key": session_key,
                "agent": session["agent"],
                "session_id": session["session_id"],
                "space_label": space_label,
                "title": session.get("title") or session["session_id"],
                "cwd": cwd,
                "path": session.get("path") or "",
                "started_at": session.get("started_at") or "",
                "updated_at": session.get("updated_at") or "",
                "indexed_at": now,
            }
        )
        meta = "\n".join(
            [
                f"archive {session['agent']}",
                f"space {space_label}",
                f"workspace {space_label}",
                f"session_id {session['session_id']}",
                f"title {session.get('title') or ''}",
                f"cwd {cwd}",
                f"path {session.get('path') or ''}",
                f"started_at {session.get('started_at') or ''}",
                f"updated_at {session.get('updated_at') or ''}",
            ]
        )
        chunks = chunk_text(session["content"], max_lines=80, overlap=10) or [session["content"][:1200]]
        for idx, chunk in enumerate(chunks):
            body = f"{meta}\n\n{chunk}"
            stable_id = "archive:" + hashlib.sha1(f"{session_key}\0{idx}\0{body}".encode("utf-8", "replace")).hexdigest()
            row = {
                "stable_id": stable_id,
                "session_key": session_key,
                "agent": session["agent"],
                "session_id": session["session_id"],
                "space_label": space_label,
                "title": session.get("title") or session["session_id"],
                "cwd": cwd,
                "path": session.get("path") or "",
                "started_at": session.get("started_at") or "",
                "updated_at": session.get("updated_at") or "",
                "chunk_index": idx,
                "content": chunk,
                "body": body,
                "indexed_at": now,
            }
            doc_rows.append(row)
            for token in set(tokens(body)):
                doc_tokens.append((token, stable_id))

    conn = connect()
    with conn:
        conn.execute("DELETE FROM archive_sessions")
        conn.execute("DELETE FROM archive_docs")
        conn.execute("DELETE FROM archive_docs_fts")
        conn.execute("DELETE FROM archive_terms")
        conn.execute("DELETE FROM archive_token_docs")
        conn.execute("DELETE FROM archive_token_trigrams")
        conn.executemany(
            """
            INSERT INTO archive_sessions (
                session_key, agent, session_id, space_label, title, cwd, path,
                started_at, updated_at, indexed_at
            )
            VALUES (
                :session_key, :agent, :session_id, :space_label, :title, :cwd, :path,
                :started_at, :updated_at, :indexed_at
            )
            """,
            session_rows,
        )
        conn.executemany(
            """
            INSERT INTO archive_docs (
                stable_id, session_key, agent, session_id, space_label, title, cwd, path,
                started_at, updated_at, chunk_index, content, indexed_at
            )
            VALUES (
                :stable_id, :session_key, :agent, :session_id, :space_label, :title, :cwd, :path,
                :started_at, :updated_at, :chunk_index, :content, :indexed_at
            )
            """,
            doc_rows,
        )
        conn.executemany(
            "INSERT INTO archive_docs_fts (stable_id, body) VALUES (:stable_id, :body)",
            doc_rows,
        )
        if doc_tokens:
            unique_terms = sorted({token for token, _stable_id in doc_tokens})
            conn.executemany(
                "INSERT OR IGNORE INTO archive_terms (token) VALUES (?)",
                [(token,) for token in unique_terms],
            )
            conn.executemany(
                "INSERT OR IGNORE INTO archive_token_docs (token, stable_id) VALUES (?, ?)",
                doc_tokens,
            )
            trigram_rows = []
            for token in unique_terms:
                for trigram in token_trigrams(token):
                    trigram_rows.append((trigram, token))
            conn.executemany(
                "INSERT OR IGNORE INTO archive_token_trigrams (trigram, token) VALUES (?, ?)",
                trigram_rows,
            )
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('last_archive_indexed_at', ?)",
            (str(now),),
        )
    conn.close()
    return len(session_rows), len(doc_rows)


def maybe_background_archive_index(agents: str, max_files, since_days, stale_seconds: int):
    require_archive_enabled()
    conn = connect()
    try:
        doc_count = conn.execute("SELECT COUNT(*) FROM archive_docs").fetchone()[0]
        last = conn.execute("SELECT value FROM meta WHERE key = 'last_archive_indexed_at'").fetchone()
    finally:
        conn.close()

    last_indexed = int(last[0]) if last else 0
    if doc_count and int(time.time()) - last_indexed < stale_seconds:
        return

    lock_fd = try_exclusive_lock(data_dir() / "archive-index.lock")
    if lock_fd is None:
        return

    cmd = [*cli_command(), "archive-index", "--agents", agents]
    if max_files is not None:
        cmd.extend(["--max-files", str(max_files)])
    if since_days is not None:
        cmd.extend(["--since-days", str(since_days)])
    spawn_locked_background(cmd, lock_fd)


def parse_filters(query: str):
    filters = {}

    def pull(name):
        nonlocal query
        values = re.findall(rf"\b{name}:([A-Za-z0-9_.:/@+-]+)", query)
        if values:
            filters[name] = values[-1]
            query = re.sub(rf"\b{name}:[A-Za-z0-9_.:/@+-]+", " ", query)

    for key in ("status", "agent", "workspace", "cwd"):
        pull(key)
    return " ".join(query.split()), filters


def fts_query(query: str) -> str:
    terms = re.findall(r"[\w./@+-]+", query.lower())
    terms = [term.strip(".+-/") for term in terms]
    terms = [term for term in terms if term]
    quoted = []
    for term in terms:
        escaped = term.replace('"', '""')
        if len(term) >= 2:
            quoted.append(f'"{escaped}"*')
        else:
            quoted.append(f'"{escaped}"')
    return " ".join(quoted)


def search_index(query: str, limit: int, *, status=None, agent=None, snippets=True, all_sessions=False):
    query, filters = parse_filters(query)
    if status:
        filters["status"] = status
    if agent:
        filters["agent"] = agent
    query_terms = tokens(query)

    clauses = []
    params = {}
    if not all_sessions:
        clauses.append("d.herdr_session = :herdr_session")
        params["herdr_session"] = herdr_session_key()
    if filters.get("status"):
        clauses.append("COALESCE(d.agent_status, '') = :status")
        params["status"] = filters["status"]
    if filters.get("agent"):
        clauses.append("COALESCE(d.agent, '') = :agent")
        params["agent"] = filters["agent"]
    if filters.get("workspace"):
        clauses.append("COALESCE(d.workspace_label, '') LIKE :workspace")
        params["workspace"] = f"%{filters['workspace']}%"
    if filters.get("cwd"):
        clauses.append("COALESCE(d.cwd, '') LIKE :cwd")
        params["cwd"] = f"%{filters['cwd']}%"

    conn = connect()
    fts = fts_query(query)
    rows = []
    if fts:
        try:
            where = ["docs_fts MATCH :fts"]
            where.extend(clauses)
            fts_params = dict(params)
            fts_params["fts"] = fts
            fts_params["limit"] = limit
            sql = f"""
                SELECT d.*, bm25(docs_fts) AS rank,
                       substr(d.content, 1, 260) AS snippet
                FROM docs_fts
                JOIN docs d ON d.stable_id = docs_fts.stable_id
                WHERE {' AND '.join(where)}
                ORDER BY rank ASC
                LIMIT :limit
            """
            rows = [dict(row) for row in conn.execute(sql, fts_params).fetchall()]
            if snippets:
                for row in rows:
                    row["snippet"] = fuzzy_snippet(row.get("content") or "", query_terms)
        except sqlite3.OperationalError:
            rows = []
    needs_typo_fuzzy = any(
        len(term) >= 4 and not has_prefix_token(conn, term)
        for term in query_terms
    )
    needs_more_results = bool(query_terms) and len(rows) < min(limit, 40)
    use_fuzzy = (not fts) or (not rows) or needs_typo_fuzzy or needs_more_results
    if use_fuzzy:
        fuzzy_rows = fuzzy_search(conn, query, clauses, params, limit * 4, snippets=snippets)
        seen = {row["stable_id"]: row for row in rows}
        for row in fuzzy_rows:
            existing = seen.get(row["stable_id"])
            if existing:
                if row.get("matched_tokens"):
                    existing["matched_tokens"] = row["matched_tokens"]
                continue
            if row["stable_id"] not in seen:
                rows.append(row)
                seen[row["stable_id"]] = row
    mark_space_matches(rows, query)
    rows.sort(key=lambda row: (space_sort_weight(row), float(row.get("rank") or 0)))
    conn.close()
    return rows[:limit]


def fuzzy_search(conn, query: str, clauses, params, limit: int, *, snippets=True):
    query_terms = list(dict.fromkeys(tokens(query)))
    if not query_terms:
        where = clauses or ["1 = 1"]
        sql = f"""
            SELECT d.*, 0.0 AS rank, substr(d.content, 1, 260) AS snippet
            FROM docs d
            WHERE {' AND '.join(where)}
            ORDER BY d.indexed_at DESC, d.workspace_label ASC, d.chunk_index ASC
            LIMIT :limit
        """
        fuzzy_params = dict(params)
        fuzzy_params["limit"] = limit
        return [dict(row) for row in conn.execute(sql, fuzzy_params).fetchall()]

    per_term_matches = []
    for term in query_terms:
        token_scores = dict(candidate_tokens_for_term(conn, term))
        if not token_scores:
            return []
        placeholders = ",".join("?" for _ in token_scores)
        rows = conn.execute(
            f"""
            SELECT token, stable_id
            FROM token_docs
            WHERE token IN ({placeholders})
            """,
            tuple(token_scores),
        ).fetchall()
        stable_matches = {}
        for token, stable_id in rows:
            score = token_scores[token]
            current = stable_matches.get(stable_id)
            if current is None or score > current[0]:
                stable_matches[stable_id] = (score, token)
        if not stable_matches:
            return []
        per_term_matches.append(stable_matches)

    common_ids = set(per_term_matches[0])
    for matches in per_term_matches[1:]:
        common_ids.intersection_update(matches)
        if not common_ids:
            return []

    scored_ids = []
    for stable_id in common_ids:
        scores = [matches[stable_id][0] for matches in per_term_matches]
        matched_tokens = [matches[stable_id][1] for matches in per_term_matches]
        min_score = min(scores)
        avg_score = sum(scores) / len(scores)
        scored_ids.append((stable_id, -((avg_score + min_score) / 2), matched_tokens))
    scored_ids.sort(key=lambda item: item[1])

    fetch_cap = min(len(scored_ids), max(limit * 4, 300), 900)
    selected = scored_ids[:fetch_cap]
    if not selected:
        return []

    id_params = {f"id{idx}": stable_id for idx, (stable_id, _rank, _tokens) in enumerate(selected)}
    id_lookup = {stable_id: (rank, matched_tokens) for stable_id, rank, matched_tokens in selected}
    id_clause = ", ".join(f":{key}" for key in id_params)
    where = [f"d.stable_id IN ({id_clause})"]
    where.extend(clauses)
    sql = f"""
        SELECT d.*
        FROM docs d
        WHERE {' AND '.join(where)}
    """
    fetched = [dict(row) for row in conn.execute(sql, {**params, **id_params}).fetchall()]
    for row in fetched:
        rank, matched_tokens = id_lookup[row["stable_id"]]
        row["rank"] = rank
        row["matched_tokens"] = matched_tokens
        row["snippet"] = fuzzy_snippet(row.get("content") or "", matched_tokens) if snippets else ""
    return sorted(fetched, key=lambda row: row["rank"])[:limit]


def fuzzy_snippet(content: str, matched_tokens):
    text = clean_text(content)
    lower = text.lower()
    positions = [lower.find(token.lower()) for token in matched_tokens if token]
    positions = [pos for pos in positions if pos >= 0]
    if positions:
        start = max(0, min(positions) - 80)
        end = min(len(text), min(positions) + 220)
        snippet = text[start:end]
        if start:
            snippet = "..." + snippet
        if end < len(text):
            snippet += "..."
        return snippet
    return text[:260]


def is_workspace_row(row) -> bool:
    return (row.get("pane_id") or "").startswith("workspace:")


def fetch_workspace_rows(workspace_ids):
    workspace_ids = [workspace_id for workspace_id in dict.fromkeys(workspace_ids) if workspace_id]
    if not workspace_ids:
        return {}
    placeholders = ",".join("?" for _ in workspace_ids)
    conn = connect()
    try:
        rows = conn.execute(
            f"""
            SELECT *
            FROM docs
            WHERE pane_id IN ({placeholders})
            """,
            tuple(f"workspace:{workspace_id}" for workspace_id in workspace_ids),
        ).fetchall()
    finally:
        conn.close()
    return {row["workspace_id"]: dict(row) for row in rows}


def decorate_live_tree(rows):
    if not rows:
        return rows
    workspace_order = []
    workspace_rows = {}
    child_rows = {}
    for row in rows:
        workspace_id = row.get("workspace_id") or ""
        if workspace_id and workspace_id not in workspace_order:
            workspace_order.append(workspace_id)
        if is_workspace_row(row):
            workspace_rows[workspace_id] = row
            continue
        child_rows.setdefault(workspace_id, []).append(row)

    missing_headers = [
        workspace_id
        for workspace_id in workspace_order
        if workspace_id and workspace_id not in workspace_rows and child_rows.get(workspace_id)
    ]
    workspace_rows.update(fetch_workspace_rows(missing_headers))

    decorated = []
    for workspace_id in workspace_order:
        header = workspace_rows.get(workspace_id)
        children = child_rows.get(workspace_id, [])
        if header:
            header = dict(header)
            header["_tree_depth"] = 0
            decorated.append(header)
        for child in children:
            child = dict(child)
            child["_tree_depth"] = 1
            decorated.append(child)
    return decorated


def grouped_search_index(query: str, limit: int, *, status=None, agent=None, snippets=True, all_sessions=False):
    rows = search_index(
        query, max(limit * 8, 120), status=status, agent=agent, snippets=snippets, all_sessions=all_sessions
    )
    grouped = {}
    for row in rows:
        pane_id = row["pane_id"]
        current = grouped.get(pane_id)
        if current is None:
            row["match_count"] = 1
            grouped[pane_id] = row
            continue
        current["match_count"] += 1
        if float(row.get("rank") or 0) < float(current.get("rank") or 0):
            row["match_count"] = current["match_count"]
            grouped[pane_id] = row

    def sort_key(row):
        return (
            space_sort_weight(row),
            float(row.get("rank") or 0),
            STATUS_WEIGHT.get(row.get("agent_status") or "unknown", 9),
            row.get("workspace_label") or "",
            row.get("pane_label") or "",
        )

    return decorate_live_tree(sorted(grouped.values(), key=sort_key)[:limit])


def mark_archive_row(row, conn=None, space_cache=None):
    row["source"] = "archive"
    row["agent_status"] = "archive"
    row["workspace_label"] = archive_space_label(row, conn=conn, cache=space_cache)
    row["pane_label"] = row.get("title") or row.get("session_id")
    row["pane_id"] = row.get("session_id")
    row["foreground_cwd"] = row.get("cwd") or ""
    return row


def strip_date_prefix(value: str) -> str:
    value = re.sub(r"^\d{8}-", "", value or "")
    for prefix in app_config()["strip_prefixes"]:
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value


def derive_space_label_from_cwd(cwd: str) -> str:
    cwd = clean_text(cwd or "")
    if not cwd:
        return "archive"
    exact_label = app_config()["exact_workspace_labels"].get(cwd)
    if exact_label:
        return exact_label
    parts = [part for part in Path(cwd).parts if part not in ("/", "")]
    if not parts:
        return "archive"
    worktree_part = ""
    for marker in app_config()["worktree_markers"]:
        if marker in parts:
            marker_index = parts.index(marker)
            if marker_index + 1 < len(parts):
                worktree_part = parts[marker_index + 1]
                break
    if worktree_part:
        label = strip_date_prefix(worktree_part)
    else:
        label = parts[-1]
    label = label.replace("_", "-").replace("-", " ")
    for word in app_config()["remove_words"]:
        label = re.sub(rf"\b{re.escape(word)}\b", "", label)
    label = " ".join(label.split())
    return label or parts[-1]


def live_space_label_for_session(agent: str, session_id: str, conn=None, cache=None):
    agent = clean_text(agent or "")
    session_id = clean_text(session_id or "")
    if not agent or not session_id:
        return ""
    key = (agent, session_id)
    if cache is not None and key in cache:
        return cache[key]
    row = None
    close_conn = conn is None
    try:
        conn = conn or connect()
        row = conn.execute(
            """
            SELECT workspace_label, COUNT(*) AS count
            FROM docs
            WHERE COALESCE(agent, '') = ?
              AND COALESCE(agent_session_id, '') = ?
              AND COALESCE(workspace_label, '') <> ''
            GROUP BY workspace_label
            ORDER BY count DESC, workspace_label ASC
            LIMIT 1
            """,
            (agent, session_id),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        if close_conn and conn is not None:
            conn.close()
    label = row["workspace_label"] if row else ""
    if cache is not None:
        cache[key] = label
    return label


def live_space_label_for_cwd(cwd: str, conn=None, cache=None):
    cwd = clean_text(cwd or "")
    if not cwd:
        return ""
    if cache is not None and cwd in cache:
        return cache[cwd]
    row = None
    close_conn = conn is None
    try:
        conn = conn or connect()
        row = conn.execute(
            """
            SELECT workspace_label, COUNT(*) AS count
            FROM docs
            WHERE cwd = ?
              AND COALESCE(workspace_label, '') <> ''
            GROUP BY workspace_label
            ORDER BY count DESC, workspace_label ASC
            LIMIT 1
            """,
            (cwd,),
        ).fetchone()
    except sqlite3.Error:
        return ""
    finally:
        if close_conn and conn is not None:
            conn.close()
    label = row["workspace_label"] if row else ""
    if cache is not None:
        cache[cwd] = label
    return label


def live_space_labels_by_session(conn):
    rows = conn.execute(
        """
        SELECT agent, agent_session_id, workspace_label, COUNT(*) AS count
        FROM docs
        WHERE COALESCE(agent, '') <> ''
          AND COALESCE(agent_session_id, '') <> ''
          AND COALESCE(workspace_label, '') <> ''
        GROUP BY agent, agent_session_id, workspace_label
        ORDER BY agent ASC, agent_session_id ASC, count DESC, workspace_label ASC
        """
    ).fetchall()
    labels = {}
    for row in rows:
        key = (row["agent"], row["agent_session_id"])
        if key not in labels:
            labels[key] = row["workspace_label"]
    return labels


def live_space_labels_by_cwd(conn):
    rows = conn.execute(
        """
        SELECT cwd, workspace_label, COUNT(*) AS count
        FROM docs
        WHERE COALESCE(cwd, '') <> ''
          AND COALESCE(workspace_label, '') <> ''
        GROUP BY cwd, workspace_label
        ORDER BY cwd ASC, count DESC, workspace_label ASC
        """
    ).fetchall()
    labels = {}
    for row in rows:
        cwd = clean_text(row["cwd"] or "")
        if cwd and cwd not in labels:
            labels[cwd] = row["workspace_label"]
    return labels


def is_archive_placeholder_label(label: str) -> bool:
    label = " ".join(clean_text(label or "").split()).lower()
    return label == "archive" or label.startswith("archive ")


def archive_space_label(row, conn=None, cache=None) -> str:
    session_cache = None
    cwd_cache = cache
    if cache is not None:
        session_cache = cache.setdefault("__session__", {})
        cwd_cache = cache.setdefault("__cwd__", {})
    live_session_label = live_space_label_for_session(
        row.get("agent") or "",
        row.get("session_id") or "",
        conn=conn,
        cache=session_cache,
    )
    if live_session_label and not is_archive_placeholder_label(live_session_label):
        return live_session_label
    live_label = live_space_label_for_cwd(row.get("cwd") or "", conn=conn, cache=cwd_cache)
    if live_label and not is_archive_placeholder_label(live_label):
        return live_label
    if row.get("space_label") and not is_archive_placeholder_label(row["space_label"]):
        return row["space_label"]
    return derive_space_label_from_cwd(row.get("cwd") or "")


def archive_search_index(query: str, limit: int, *, agent=None, snippets=True):
    query, filters = parse_filters(query)
    if agent:
        filters["agent"] = agent
    query_terms = tokens(query)

    clauses = []
    params = {}
    if filters.get("agent"):
        clauses.append("COALESCE(d.agent, '') = :agent")
        params["agent"] = filters["agent"]
    if filters.get("cwd"):
        clauses.append("COALESCE(d.cwd, '') LIKE :cwd")
        params["cwd"] = f"%{filters['cwd']}%"
    if filters.get("workspace"):
        clauses.append(
            "(COALESCE(d.space_label, '') LIKE :workspace OR COALESCE(d.title, '') LIKE :workspace OR COALESCE(d.cwd, '') LIKE :workspace)"
        )
        params["workspace"] = f"%{filters['workspace']}%"

    conn = connect()
    space_cache = {}
    fts = fts_query(query)
    rows = []
    if fts:
        try:
            where = ["archive_docs_fts MATCH :fts"]
            where.extend(clauses)
            fts_params = dict(params)
            fts_params["fts"] = fts
            fts_params["limit"] = limit
            sql = f"""
                SELECT d.*, bm25(archive_docs_fts) AS rank,
                       substr(d.content, 1, 320) AS snippet
                FROM archive_docs_fts
                JOIN archive_docs d ON d.stable_id = archive_docs_fts.stable_id
                WHERE {' AND '.join(where)}
                ORDER BY rank ASC
                LIMIT :limit
            """
            rows = [mark_archive_row(dict(row), conn=conn, space_cache=space_cache) for row in conn.execute(sql, fts_params).fetchall()]
            if snippets:
                for row in rows:
                    row["snippet"] = fuzzy_snippet(row.get("content") or "", query_terms)
        except sqlite3.OperationalError:
            rows = []

    needs_typo_fuzzy = any(
        len(term) >= 4 and not has_prefix_token(conn, term, scope="archive")
        for term in query_terms
    )
    needs_more_results = bool(query_terms) and len(rows) < min(limit, 40)
    use_fuzzy = (not fts) or (not rows) or needs_typo_fuzzy or needs_more_results
    if use_fuzzy:
        fuzzy_rows = archive_fuzzy_search(conn, query, clauses, params, min(limit, 80), snippets=snippets)
        seen = {row["stable_id"]: row for row in rows}
        for row in fuzzy_rows:
            existing = seen.get(row["stable_id"])
            if existing:
                if row.get("matched_tokens"):
                    existing["matched_tokens"] = row["matched_tokens"]
                continue
            rows.append(row)
            seen[row["stable_id"]] = row
    mark_space_matches(rows, query)
    rows.sort(key=lambda row: (space_sort_weight(row), float(row.get("rank") or 0), -iso_to_epoch(row.get("updated_at") or "")))
    conn.close()
    return rows[:limit]


def archive_fuzzy_search(conn, query: str, clauses, params, limit: int, *, snippets=True):
    query_terms = list(dict.fromkeys(tokens(query)))
    space_cache = {}
    if not query_terms:
        where = clauses or ["1 = 1"]
        sql = f"""
            SELECT d.*, 0.0 AS rank, substr(d.content, 1, 320) AS snippet
            FROM archive_docs d
            WHERE {' AND '.join(where)}
            ORDER BY d.updated_at DESC, d.title ASC, d.chunk_index ASC
            LIMIT :limit
        """
        fuzzy_params = dict(params)
        fuzzy_params["limit"] = limit
        return [mark_archive_row(dict(row), conn=conn, space_cache=space_cache) for row in conn.execute(sql, fuzzy_params).fetchall()]

    per_term_matches = []
    for term in query_terms:
        token_scores = dict(candidate_tokens_for_term(conn, term, scope="archive"))
        if not token_scores:
            return []
        placeholders = ",".join("?" for _ in token_scores)
        rows = conn.execute(
            f"""
            SELECT token, stable_id
            FROM archive_token_docs
            WHERE token IN ({placeholders})
            """,
            tuple(token_scores),
        ).fetchall()
        stable_matches = {}
        for token, stable_id in rows:
            score = token_scores[token]
            current = stable_matches.get(stable_id)
            if current is None or score > current[0]:
                stable_matches[stable_id] = (score, token)
        if not stable_matches:
            return []
        per_term_matches.append(stable_matches)

    common_ids = set(per_term_matches[0])
    for matches in per_term_matches[1:]:
        common_ids.intersection_update(matches)
        if not common_ids:
            return []

    scored_ids = []
    for stable_id in common_ids:
        scores = [matches[stable_id][0] for matches in per_term_matches]
        matched_tokens = [matches[stable_id][1] for matches in per_term_matches]
        min_score = min(scores)
        avg_score = sum(scores) / len(scores)
        scored_ids.append((stable_id, -((avg_score + min_score) / 2), matched_tokens))
    scored_ids.sort(key=lambda item: item[1])

    selected = scored_ids[: min(len(scored_ids), max(limit * 4, 300), 1200)]
    if not selected:
        return []

    id_params = {f"id{idx}": stable_id for idx, (stable_id, _rank, _tokens) in enumerate(selected)}
    id_lookup = {stable_id: (rank, matched_tokens) for stable_id, rank, matched_tokens in selected}
    id_clause = ", ".join(f":{key}" for key in id_params)
    where = [f"d.stable_id IN ({id_clause})"]
    where.extend(clauses)
    sql = f"""
        SELECT d.*
        FROM archive_docs d
        WHERE {' AND '.join(where)}
    """
    fetched = [dict(row) for row in conn.execute(sql, {**params, **id_params}).fetchall()]
    for row in fetched:
        rank, matched_tokens = id_lookup[row["stable_id"]]
        row["rank"] = rank
        row["matched_tokens"] = matched_tokens
        row["snippet"] = fuzzy_snippet(row.get("content") or "", matched_tokens) if snippets else ""
        mark_archive_row(row, conn=conn, space_cache=space_cache)
    return sorted(fetched, key=lambda row: (row["rank"], -iso_to_epoch(row.get("updated_at") or "")))[:limit]


def grouped_archive_search_index(query: str, limit: int, *, agent=None, snippets=True):
    rows = archive_search_index(query, max(limit * 2, 50), agent=agent, snippets=snippets)
    grouped = {}
    for row in rows:
        session_key = row["session_key"]
        current = grouped.get(session_key)
        if current is None:
            row["match_count"] = 1
            grouped[session_key] = row
            continue
        current["match_count"] += 1
        if float(row.get("rank") or 0) < float(current.get("rank") or 0):
            row["match_count"] = current["match_count"]
            grouped[session_key] = row

    def sort_key(row):
        return (
            space_sort_weight(row),
            float(row.get("rank") or 0),
            -iso_to_epoch(row.get("updated_at") or ""),
            row.get("title") or "",
        )

    return sorted(grouped.values(), key=sort_key)[:limit]


def shorten(value: str, width: int) -> str:
    value = " ".join(clean_text(value or "").split())
    if len(value) <= width:
        return value
    if width <= 1:
        return value[:width]
    return value[: width - 1] + "…"


def display_status(row) -> str:
    if is_workspace_row(row):
        return "workspace"
    return row.get("agent_status") or "unknown"


def tree_indent(row) -> str:
    return "  " if int(row.get("_tree_depth") or 0) > 0 else ""


def format_result(row, *, multiline=False):
    status = display_status(row)
    workspace = row.get("workspace_label") or row.get("workspace_id")
    is_workspace = is_workspace_row(row)
    label = row.get("pane_label") or row.get("pane_id")
    agent = row.get("agent") or "shell"
    cwd = row.get("cwd") or ""
    snippet_value = row.get("snippet")
    if snippet_value is None:
        snippet_value = row.get("content") or ""
    snippet = clean_text(snippet_value or "")
    snippet = " ".join(snippet.split())
    if is_workspace:
        title = f"[{status}] {workspace}"
    else:
        title = f"{tree_indent(row)}[{status}] {workspace} / {agent} / {label}"
    if multiline:
        return (
            f"{row['stable_id']}\t{title}\n"
            f"  {cwd}\n"
            f"  {snippet[:500]}"
        )
    matches = row.get("match_count")
    count = f" x{matches}" if matches and int(matches) > 1 else ""
    left = f"{title}{count}"
    return "\t".join(
        [
            row["stable_id"],
            shorten(left, 56),
            shorten(cwd, 58),
            shorten(snippet, 110),
        ]
    )


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
    row = dict(row)
    if is_workspace_row(row):
        return 0 if focus_workspace_tab(row) else 1
    if focus_exact_pane(row):
        return 0
    print(f"could not focus exact pane: {row.get('pane_id') or stable_id}", file=sys.stderr)
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


def focus_archive_result(stable_id: str) -> int:
    conn = connect()
    row = conn.execute(
        """
        SELECT s.*
        FROM archive_docs d
        JOIN archive_sessions s ON s.session_key = d.session_key
        WHERE d.stable_id = ?
        """,
        (stable_id,),
    ).fetchone()
    conn.close()
    if not row:
        print(f"unknown archive result id: {stable_id}", file=sys.stderr)
        return 2
    row = dict(row)
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


def pick(args) -> int:
    if args.refresh:
        count = index_session(args.lines, args.include_empty, args.include_wrappers)
        if args.verbose:
            print(f"indexed {count} chunks", file=sys.stderr)
    elif args.background_refresh:
        maybe_background_index(args.lines, args.include_empty, args.include_wrappers, args.stale_seconds)
    if args.native or (not args.fzf and sys.stdin.isatty() and sys.stdout.isatty()):
        return curses.wrapper(lambda stdscr: curses_picker(stdscr, args))
    return fzf_picker(args)


def archive_pick(args) -> int:
    args.archive = True
    if args.refresh:
        sessions, chunks = index_archive(args.agents, args.max_files, args.since_days)
        if args.verbose:
            print(f"indexed {sessions} archived sessions / {chunks} chunks", file=sys.stderr)
    elif args.background_refresh:
        maybe_background_archive_index(args.agents, args.max_files, args.since_days, args.stale_seconds)
    if args.native or (not args.fzf and sys.stdin.isatty() and sys.stdout.isatty()):
        return curses.wrapper(lambda stdscr: curses_picker(stdscr, args))
    return fzf_picker(args)


def fzf_picker(args) -> int:
    rows = picker_rows(args, " ".join(args.query))
    if not rows:
        print("No OmniSearch matches.", file=sys.stderr)
        return 1
    fzf = shutil.which("fzf")
    if not fzf:
        for row in rows:
            print(format_result(row, multiline=True))
        return 1
    input_text = "\n".join(format_result(row) for row in rows)
    proc = subprocess.run(
        [
            fzf,
            "--delimiter",
            "\t",
            "--with-nth",
            "2,3,4",
            "--nth",
            "2,3,4",
            "--preview",
            f"{cli_command_string()} preview {{1}}",
            "--preview-window",
            "down,45%,wrap",
            "--header",
            picker_help(args),
            "--border",
            "rounded",
            "--border-label",
            f" {picker_title(args)} ",
            "--preview-label",
            " context ",
            "--highlight-line",
            "--track",
            "--prompt",
            f"{picker_title(args)}> ",
            "--height",
            "100%",
            "--layout",
            "reverse",
        ],
        input=input_text,
        text=True,
        stdout=subprocess.PIPE,
    )
    if proc.returncode != 0 or not proc.stdout.strip():
        return proc.returncode
    stable_id = proc.stdout.split("\t", 1)[0].strip()
    return picker_focus(args, stable_id)


def addnstr_safe(stdscr, y, x, text, width, attr=0):
    if y < 0 or x < 0 or width <= 0:
        return
    try:
        stdscr.addnstr(y, x, text, width, attr)
    except curses.error:
        pass


def status_attr(status):
    if status == "archive":
        return curses.color_pair(1)
    if status == "workspace":
        return curses.color_pair(1) | curses.A_BOLD
    if status == "working":
        return curses.color_pair(2) | curses.A_BOLD
    if status == "blocked":
        return curses.color_pair(3) | curses.A_BOLD
    if status == "idle":
        return curses.color_pair(4)
    return curses.color_pair(5)


def init_curses_colors():
    try:
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_CYAN, -1)
        curses.init_pair(2, curses.COLOR_GREEN, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        curses.init_pair(4, curses.COLOR_BLUE, -1)
        curses.init_pair(5, curses.COLOR_YELLOW, -1)
        curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)
        curses.init_pair(7, curses.COLOR_BLACK, curses.COLOR_YELLOW)
    except curses.error:
        pass


def row_title(row):
    if row.get("source") == "archive":
        agent = row.get("agent") or "agent"
        space = row.get("workspace_label") or "archive"
        date = (row.get("updated_at") or row.get("started_at") or "archive")[:10]
        title = row.get("title") or row.get("session_id") or "session"
        matches = row.get("match_count")
        count = f" x{matches}" if matches and int(matches) > 1 else ""
        return f"[archive] {space} / {agent} / {title} / {date}{count}"
    if is_workspace_row(row):
        workspace = row.get("workspace_label") or row.get("workspace_id")
        matches = row.get("match_count")
        count = f" x{matches}" if matches and int(matches) > 1 else ""
        return f"[workspace] {workspace}{count}"
    label = row.get("pane_label") or row.get("pane_id")
    agent = row.get("agent") or "shell"
    status = display_status(row)
    workspace = row.get("workspace_label") or row.get("workspace_id")
    matches = row.get("match_count")
    count = f" x{matches}" if matches and int(matches) > 1 else ""
    return f"{tree_indent(row)}[{status}] {workspace} / {agent} / {label}{count}"


def query_terms_for_display(query: str):
    stripped_query, _filters = parse_filters(query)
    return tokens(stripped_query)


def highlight_terms(row, query: str):
    query_terms = query_terms_for_display(query)
    if not query_terms:
        return []
    terms = set(query_terms)
    for token in row.get("matched_tokens") or []:
        if token:
            terms.add(token.lower())
    return sorted(terms, key=len, reverse=True)


def find_highlight_spans(text: str, terms):
    spans = []
    lowered = text.lower()
    for term in terms:
        term = (term or "").lower()
        if len(term) < 2:
            continue
        start = 0
        while True:
            idx = lowered.find(term, start)
            if idx < 0:
                break
            spans.append((idx, idx + len(term)))
            start = idx + max(1, len(term))
    if not spans:
        return []
    spans.sort()
    merged = []
    for start, end in spans:
        if not merged or start > merged[-1][1]:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def add_highlighted(stdscr, y, x, text, width, base_attr=0, highlight_attr=0, terms=None):
    if y < 0 or x < 0 or width <= 0:
        return
    text = text[:width]
    spans = find_highlight_spans(text, terms or [])
    if not spans:
        addnstr_safe(stdscr, y, x, text, width, base_attr)
        return
    cursor = 0
    col = x
    for start, end in spans:
        if start > cursor:
            segment = text[cursor:start]
            addnstr_safe(stdscr, y, col, segment, width - (col - x), base_attr)
            col += len(segment)
        segment = text[start:end]
        addnstr_safe(stdscr, y, col, segment, width - (col - x), highlight_attr or (base_attr | curses.A_BOLD))
        col += len(segment)
        cursor = end
    if cursor < len(text):
        addnstr_safe(stdscr, y, col, text[cursor:], width - (col - x), base_attr)


def mode_title(mode: str) -> str:
    if mode == "normal":
        return "NORMAL"
    if mode == "action":
        return "ACTION"
    return "INSERT"


def render_picker(
    stdscr,
    query,
    rows,
    selected,
    *,
    title="Herdr OmniSearch",
    help_text=None,
    mode="insert",
    action_query="",
    actions=None,
    action_selected=0,
    message="",
):
    stdscr.erase()
    height, width = stdscr.getmaxyx()
    if height < 10 or width < 60:
        addnstr_safe(stdscr, 0, 0, f"{title} needs a larger terminal.", width - 1, curses.A_BOLD)
        stdscr.refresh()
        return

    list_height = max(5, min(height - 7, height // 2))
    preview_y = list_height + 4
    preview_height = max(1, height - preview_y - 1)

    addnstr_safe(stdscr, 0, 0, title, width - 1, curses.color_pair(1) | curses.A_BOLD)
    help_text = help_text or "Type to search chats | filters: status:working agent:codex workspace:api cwd:backend | Enter focus | Esc quit"
    addnstr_safe(stdscr, 1, 0, help_text, width - 1, curses.A_DIM)
    if mode == "action":
        prompt = f"-- {mode_title(mode)} -- :{action_query}"
    else:
        prompt = f"-- {mode_title(mode)} -- / {query}"
    addnstr_safe(stdscr, 2, 0, prompt, width - 1, curses.A_BOLD)
    if message:
        addnstr_safe(stdscr, 3, 0, shorten(message, width - 1), width - 1, curses.A_DIM)

    if not rows:
        addnstr_safe(stdscr, 4, 0, "No matches.", width - 1, curses.A_DIM)
        stdscr.refresh()
        return

    selected = max(0, min(selected, len(rows) - 1))
    visible = rows[:list_height]
    if selected >= list_height:
        start = selected - list_height + 1
        visible = rows[start : start + list_height]
    else:
        start = 0

    for offset, row in enumerate(visible):
        idx = start + offset
        y = 4 + offset
        selected_row = idx == selected
        attr = curses.color_pair(6) if selected_row else status_attr(display_status(row))
        terms = highlight_terms(row, query)
        highlight_attr = curses.color_pair(7) | curses.A_BOLD
        add_highlighted(stdscr, y, 0, row_title(row), width - 1, attr, highlight_attr, terms)
        cwd = row.get("cwd") or ""
        if width > 100:
            cwd_x = min(62, width // 2)
            add_highlighted(
                stdscr,
                y,
                cwd_x,
                shorten(cwd, width - cwd_x - 1),
                width - cwd_x - 1,
                attr,
                highlight_attr,
                terms,
            )

    addnstr_safe(stdscr, preview_y - 2, 0, "─" * max(0, width - 1), width - 1, curses.A_DIM)
    row = rows[selected]
    terms = highlight_terms(row, query)
    highlight_attr = curses.color_pair(7) | curses.A_BOLD
    preview_header = f"{row_title(row)} | {row.get('cwd') or ''}"
    add_highlighted(stdscr, preview_y - 1, 0, preview_header, width - 1, curses.A_BOLD, highlight_attr, terms)
    if mode == "action":
        actions = actions or []
        if not actions:
            addnstr_safe(stdscr, preview_y, 0, "No actions.", width - 1, curses.A_DIM)
            stdscr.refresh()
            return
        action_selected = max(0, min(action_selected, len(actions) - 1))
        for offset, action in enumerate(actions[:preview_height]):
            attr = curses.color_pair(6) if offset == action_selected else 0
            prefix = "> " if offset == action_selected else "  "
            line = f"{prefix}{action['name']:<18} {action['label']}"
            add_highlighted(stdscr, preview_y + offset, 0, line, width - 1, attr, highlight_attr, tokens(action_query))
        stdscr.refresh()
        return
    preview_lines = clean_text(row.get("content") or "").splitlines()
    for offset, line in enumerate(preview_lines[:preview_height]):
        add_highlighted(stdscr, preview_y + offset, 0, line, width - 1, 0, highlight_attr, terms)
    stdscr.refresh()


def picker_rows(args, query, *, snippets=False):
    if getattr(args, "archive", False):
        return grouped_archive_search_index(query, args.limit, agent=args.agent, snippets=snippets)
    return grouped_search_index(
        query,
        args.limit,
        status=getattr(args, "status", None),
        agent=args.agent,
        snippets=snippets,
        all_sessions=getattr(args, "all_sessions", False),
    )


def picker_focus(args, stable_id):
    if getattr(args, "archive", False):
        return focus_archive_result(stable_id)
    return focus_result(stable_id)


def picker_title(args):
    return "Herdr ArchiveSearch" if getattr(args, "archive", False) else "Herdr OmniSearch"


def picker_help(args):
    return "insert: type search | Esc normal | normal: j/k gg G Enter focus a/: actions q quit"


def clipboard_copy(text: str):
    text = text or ""
    if not text:
        return False, "nothing to yank"
    commands = [
        ["wl-copy"],
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["pbcopy"],
    ]
    for command in commands:
        if not shutil.which(command[0]):
            continue
        try:
            proc = subprocess.run(
                command,
                input=text,
                text=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=1,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0:
            return True, f"yanked {len(text)} chars"

    # OSC52 works well through many terminal setups, including Kitty, and avoids
    # adding a hard clipboard dependency.
    try:
        encoded = base64.b64encode(text.encode("utf-8")).decode("ascii")
        with open("/dev/tty", "w", encoding="utf-8", errors="ignore") as tty:
            tty.write(f"\x1b]52;c;{encoded}\a")
            tty.flush()
        return True, f"yanked {len(text)} chars via OSC52"
    except OSError:
        return False, "no clipboard path available"


def action_specs(args, row):
    archive = row.get("source") == "archive" or getattr(args, "archive", False)
    specs = []
    focus_label = "focus existing or resume archive session" if archive else "focus exact selected row"
    specs.append({"name": "focus", "label": focus_label})

    if not archive:
        if row.get("workspace_id"):
            specs.append({"name": "rename-workspace", "label": "rename workspace"})
        if row.get("pane_id") and not is_workspace_row(row):
            specs.append({"name": "rename-pane", "label": "rename pane"})

    yank_values = [
        ("yank-cwd", "yank cwd", row.get("cwd") or row.get("foreground_cwd") or ""),
        ("yank-session", "yank agent session id", row.get("session_id") or row.get("agent_session_id") or ""),
        ("yank-pane", "yank pane id", "" if archive or is_workspace_row(row) else row.get("pane_id") or ""),
        ("yank-workspace", "yank workspace id", row.get("workspace_id") or ""),
    ]
    for name, label, value in yank_values:
        if value:
            specs.append({"name": name, "label": label, "value": value})
    return specs


def filter_actions(actions, query: str):
    terms = tokens(query)
    if not terms:
        return actions
    filtered = []
    for action in actions:
        haystack = f"{action['name']} {action['label']}"
        haystack_tokens = set(tokens(haystack))
        if all(any(score_token_candidate(term, token) >= fuzzy_score_threshold(term) for token in haystack_tokens) for term in terms):
            filtered.append(action)
    return filtered


def prompt_line(stdscr, prompt: str, initial: str = ""):
    value = initial or ""
    while True:
        height, width = stdscr.getmaxyx()
        line = f"{prompt}{value}"
        addnstr_safe(stdscr, height - 1, 0, " " * max(0, width - 1), width - 1, 0)
        addnstr_safe(stdscr, height - 1, 0, line, width - 1, curses.A_BOLD)
        try:
            stdscr.move(height - 1, min(len(line), width - 2))
        except curses.error:
            pass
        stdscr.refresh()
        key = stdscr.get_wch()
        if key in ("\x03", "\x1b"):
            return None
        if key in ("\n", "\r"):
            return value.strip()
        if key in ("\x15",):
            value = ""
            continue
        if key in ("\x7f", "\b", "\x08", "\x1f", "\x04"):
            value = value[:-1]
            continue
        if isinstance(key, int):
            if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                value = value[:-1]
            continue
        if isinstance(key, str) and key.isprintable():
            value += key


def refresh_picker_index(args):
    if getattr(args, "archive", False):
        return
    index_session(
        getattr(args, "lines", DEFAULT_LINES),
        getattr(args, "include_empty", False),
        getattr(args, "include_wrappers", False),
    )


def execute_action(stdscr, args, row, action):
    name = action["name"]
    if name == "focus":
        return picker_focus(args, row["stable_id"]), "", False
    if name.startswith("yank-"):
        ok, message = clipboard_copy(action.get("value") or "")
        return None, message if ok else f"yank failed: {message}", False
    if name == "rename-workspace":
        label = prompt_line(stdscr, "workspace name: ", row.get("workspace_label") or "")
        if not label:
            return None, "rename cancelled", False
        try:
            row_client(row).rename_workspace(row["workspace_id"], label)
        except HerdrError as exc:
            return None, f"workspace rename failed: {exc}", False
        refresh_picker_index(args)
        return None, f"renamed workspace to {label}", True
    if name == "rename-pane":
        label = prompt_line(stdscr, "pane name: ", row.get("pane_label") or "")
        if not label:
            return None, "rename cancelled", False
        try:
            row_client(row).rename_pane(row["pane_id"], label)
        except HerdrError as exc:
            return None, f"pane rename failed: {exc}", False
        refresh_picker_index(args)
        return None, f"renamed pane to {label}", True
    return None, f"unknown action: {name}", False


def pending_keys(stdscr):
    keys = []
    stdscr.nodelay(True)
    try:
        while True:
            try:
                key = stdscr.get_wch()
                keys.append(key)
                if key == "\x1b":
                    break
            except curses.error:
                break
    finally:
        stdscr.nodelay(False)
    return keys


def apply_insert_key(query: str, key):
    if key == "\x1b":
        return query, False, True
    if key in ("\x15",):
        return "", True, False
    if key in ("\x7f", "\b", "\x08", "\x1f", "\x04"):
        return query[:-1], True, False
    if isinstance(key, int):
        if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
            return query[:-1], True, False
        return query, False, False
    if isinstance(key, str) and key.isprintable():
        return query + key, True, False
    return query, False, False


def cached_picker_rows(args, query, cache):
    cache_key = (
        bool(getattr(args, "archive", False)),
        getattr(args, "status", None),
        getattr(args, "agent", None),
        args.limit,
        query,
    )
    rows = cache.get(cache_key)
    if rows is None:
        rows = picker_rows(args, query, snippets=False)
        cache[cache_key] = rows
        if len(cache) > 128:
            cache.pop(next(iter(cache)))
    return rows


def curses_picker(stdscr, args) -> int:
    init_curses_colors()
    curses.noecho()
    curses.cbreak()
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    stdscr.keypad(True)
    query = " ".join(args.query)
    selected = 0
    row_cache = {}
    rows = cached_picker_rows(args, query, row_cache)
    mode = "insert"
    action_query = ""
    action_selected = 0
    pending = ""
    message = ""

    while True:
        if selected >= len(rows):
            selected = max(0, len(rows) - 1)
        selected_row = rows[selected] if rows else {}
        actions = filter_actions(action_specs(args, selected_row), action_query) if mode == "action" and rows else []
        if action_selected >= len(actions):
            action_selected = max(0, len(actions) - 1)
        render_picker(
            stdscr,
            query,
            rows,
            selected,
            title=picker_title(args),
            help_text=picker_help(args),
            mode=mode,
            action_query=action_query,
            actions=actions,
            action_selected=action_selected,
            message=message,
        )
        message = ""
        key = stdscr.get_wch()

        if key == "\x03":
            return 130
        if key == "\x1b":
            if mode == "insert":
                mode = "normal"
                pending = ""
                continue
            if mode == "action":
                mode = "normal"
                action_query = ""
                action_selected = 0
                pending = ""
                continue
            return 0

        if mode == "action":
            if key in ("\n", "\r"):
                if actions:
                    exit_code, action_message, refresh = execute_action(stdscr, args, rows[selected], actions[action_selected])
                    if exit_code is not None:
                        return exit_code
                    if refresh:
                        row_cache.clear()
                        rows = cached_picker_rows(args, query, row_cache)
                        selected = min(selected, max(0, len(rows) - 1))
                    message = action_message
                    mode = "normal"
                    action_query = ""
                    action_selected = 0
                continue
            if key in ("\x15",):
                action_query = ""
                action_selected = 0
                continue
            if key in ("\x7f", "\b", "\x08", "\x1f", "\x04"):
                action_query = action_query[:-1]
                action_selected = 0
                continue
            if isinstance(key, int):
                if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                    action_query = action_query[:-1]
                    action_selected = 0
                elif key == curses.KEY_UP:
                    action_selected = max(0, action_selected - 1)
                elif key == curses.KEY_DOWN:
                    action_selected = min(max(0, len(actions) - 1), action_selected + 1)
                continue
            if isinstance(key, str):
                if key in ("j", "\x0e"):
                    action_selected = min(max(0, len(actions) - 1), action_selected + 1)
                    continue
                if key in ("k", "\x10"):
                    action_selected = max(0, action_selected - 1)
                    continue
                if key.isprintable():
                    action_query += key
                    action_selected = 0
                continue

        if key in ("\n", "\r"):
            if rows:
                return picker_focus(args, rows[selected]["stable_id"])
            continue

        if isinstance(key, int):
            if key in (curses.KEY_BACKSPACE, curses.KEY_DC):
                if mode == "insert":
                    query = query[:-1]
                    selected = 0
                    rows = cached_picker_rows(args, query, row_cache)
            elif key == curses.KEY_UP:
                selected = max(0, selected - 1)
            elif key == curses.KEY_DOWN:
                selected = min(max(0, len(rows) - 1), selected + 1)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - 10)
            elif key == curses.KEY_NPAGE:
                selected = min(max(0, len(rows) - 1), selected + 10)
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = max(0, len(rows) - 1)
            elif key == curses.KEY_RESIZE:
                pass
            continue

        if mode == "normal" and isinstance(key, str):
            if pending == "g":
                pending = ""
                if key == "g":
                    selected = 0
                    continue
            if key == "g":
                pending = "g"
                continue
            pending = ""
            if key in ("q",):
                return 0
            if key in ("i", "/"):
                mode = "insert"
                continue
            if key == "c":
                query = ""
                selected = 0
                rows = cached_picker_rows(args, query, row_cache)
                mode = "insert"
                continue
            if key in ("a", ":"):
                mode = "action"
                action_query = ""
                action_selected = 0
                continue
            if key in ("j", "\x0e"):
                selected = min(max(0, len(rows) - 1), selected + 1)
                continue
            if key in ("k", "\x10"):
                selected = max(0, selected - 1)
                continue
            if key == "\x04":
                selected = min(max(0, len(rows) - 1), selected + 10)
                continue
            if key == "\x15":
                selected = max(0, selected - 10)
                continue
            if key == "G":
                selected = max(0, len(rows) - 1)
                continue
            continue

        if mode == "insert":
            query, changed, exit_insert = apply_insert_key(query, key)
            for queued_key in pending_keys(stdscr):
                query, queued_changed, queued_exit_insert = apply_insert_key(query, queued_key)
                changed = changed or queued_changed
                if queued_exit_insert:
                    exit_insert = True
                    break
            if exit_insert:
                mode = "normal"
                pending = ""
            if changed:
                selected = 0
                rows = cached_picker_rows(args, query, row_cache)


def watcher_pid_path() -> Path:
    return data_dir() / f"watch-{herdr_session_key()}.pid"


def watcher_log_path() -> Path:
    return data_dir() / f"watch-{herdr_session_key()}.log"


def read_watcher_pid() -> int:
    try:
        fields = watcher_pid_path().read_text(encoding="utf-8").split()
        return int(fields[0])
    except (IndexError, OSError, ValueError):
        return 0


def process_is_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def watcher_subscriptions(snapshot):
    subscriptions = [
        {"type": event}
        for event in (
            "workspace.created",
            "workspace.updated",
            "workspace.renamed",
            "workspace.closed",
            "workspace.focused",
            "tab.created",
            "tab.closed",
            "tab.focused",
            "tab.renamed",
            "tab.moved",
            "pane.created",
            "pane.closed",
            "pane.focused",
            "pane.moved",
            "pane.exited",
            "pane.agent_detected",
        )
    ]
    for pane in snapshot.get("panes", []):
        pane_id = pane.get("pane_id")
        if not pane_id:
            continue
        subscriptions.append({"type": "pane.agent_status_changed", "pane_id": pane_id})
        subscriptions.append({"type": "pane.scroll_changed", "pane_id": pane_id})
    return subscriptions


def watcher_is_running() -> bool:
    return lock_is_held(watcher_pid_path())


def stop_watcher_at(pid_path: Path) -> None:
    """Terminate the watcher process holding this pid file's lock, if any."""
    if not lock_is_held(pid_path):
        return
    try:
        pid = int(pid_path.read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return
    if pid > 0 and pid != os.getpid():
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def stop_legacy_watcher() -> None:
    # Watchers from releases before per-session scoping rebuilt the whole
    # docs table and would clobber other sessions; stop them on upgrade.
    legacy = data_dir() / "watch.pid"
    if legacy != watcher_pid_path():
        stop_watcher_at(legacy)


def watch_live_index(lines: int, debounce: float) -> int:
    # The flock outlives any crash, so liveness never depends on stale pid
    # contents; the pid is only written for status messages and watch-stop.
    stop_legacy_watcher()
    lock_fd = try_exclusive_lock(watcher_pid_path())
    if lock_fd is None:
        print(f"watcher already running: {read_watcher_pid()}")
        return 0
    retry_delay = 1.0
    try:
        while True:
            try:
                snapshot = HerdrClient().snapshot()
                index_session(lines, False, False, snapshot=snapshot)
                retry_delay = 1.0
                subscriptions = watcher_subscriptions(snapshot)
                reconnect = False
                refresh_due = None
                with HerdrClient() as events:
                    events.subscribe(subscriptions)
                    while True:
                        try:
                            event = events.next_event(timeout=1.0)
                        except HerdrTimeout:
                            event = None
                        now = time.monotonic()
                        if event:
                            if refresh_due is None:
                                refresh_due = now + debounce
                            event_name = event.get("event") or ""
                            reconnect = reconnect or event_name in {"pane.created", "pane.closed"}
                        if refresh_due is not None and now >= refresh_due:
                            index_session(lines, False, False)
                            refresh_due = None
                            if reconnect:
                                break
            except (HerdrError, HerdrCLIError, sqlite3.Error, OSError) as exc:
                print(
                    f"watch retry in {retry_delay:g}s after {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 30.0)
    finally:
        os.close(lock_fd)


def cmd_watch(args) -> int:
    return watch_live_index(args.lines, args.debounce)


def cmd_watch_start(args) -> int:
    if watcher_is_running():
        print(f"watcher running: {read_watcher_pid()}")
        return 0
    command = [*cli_command(), "watch", "--lines", str(args.lines), "--debounce", str(args.debounce)]
    env = os.environ.copy()
    with watcher_log_path().open("ab") as log:
        proc = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            env=env,
            start_new_session=True,
        )
    for _ in range(20):
        if watcher_is_running():
            print(f"watcher started: {read_watcher_pid()}")
            return 0
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    print(f"watcher failed to start; see {watcher_log_path()}", file=sys.stderr)
    return 1


def cmd_watch_stop(_args) -> int:
    if not watcher_is_running():
        print("watcher stopped")
        return 0
    pid = read_watcher_pid()
    if pid:
        os.kill(pid, 15)
    for _ in range(40):
        if not watcher_is_running():
            print("watcher stopped")
            return 0
        time.sleep(0.05)
    print(f"watcher did not stop: {pid}", file=sys.stderr)
    return 1


def cmd_watch_status(_args) -> int:
    if watcher_is_running():
        print(f"watcher running: {read_watcher_pid()}")
        return 0
    print("watcher stopped")
    return 1


def cmd_event_refresh(_args) -> int:
    if watcher_is_running():
        return 0
    maybe_background_index(DEFAULT_LINES, False, False, stale_seconds=2)
    return 0


def cmd_open_plugin_pane(args) -> int:
    result = HerdrClient().open_plugin_pane(args.entrypoint)
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_index(args) -> int:
    try:
        count = index_session(args.lines, args.include_empty, args.include_wrappers)
        print(f"indexed {count} chunks into {db_path()}")
        return 0
    finally:
        release_index_lock()


def cmd_search(args) -> int:
    rows = grouped_search_index(
        " ".join(args.query),
        args.limit,
        status=args.status,
        agent=args.agent,
        all_sessions=args.all_sessions,
    )
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        print(format_result(row, multiline=True))
        print()
    return 0


def cmd_archive_index(args) -> int:
    try:
        sessions, chunks = index_archive(args.agents, args.max_files, args.since_days)
        print(f"indexed {sessions} archived sessions / {chunks} chunks into {db_path()}")
        return 0
    finally:
        release_index_lock()


def cmd_archive_search(args) -> int:
    rows = grouped_archive_search_index(" ".join(args.query), args.limit, agent=args.agent)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    for row in rows:
        print(format_result(row, multiline=True))
        print()
    return 0


def cmd_focus(args) -> int:
    return focus_result(args.stable_id)


def cmd_archive_resume(args) -> int:
    return focus_archive_result(args.stable_id)


def cmd_preview(args) -> int:
    conn = connect()
    row = conn.execute("SELECT * FROM docs WHERE stable_id = ?", (args.stable_id,)).fetchone()
    if not row:
        row = conn.execute("SELECT * FROM archive_docs WHERE stable_id = ?", (args.stable_id,)).fetchone()
    conn.close()
    if not row:
        return 1
    row = dict(row)
    if row.get("session_key"):
        mark_archive_row(row)
    label = row.get("pane_label") or row.get("pane_id")
    agent = row.get("agent") or "shell"
    status = display_status(row)
    workspace = row.get("workspace_label") or row.get("workspace_id")
    print(f"{workspace} / {agent} / {label} [{status}]")
    print(row.get("cwd") or "")
    print()
    print(clean_text(row.get("content") or ""))
    return 0


def cmd_doctor(_args) -> int:
    print(f"db: {db_path()}")
    print(f"herdr: {herdr_bin()}")
    print(f"herdr_socket: {resolve_socket_path()}")
    print(f"herdr_session: {herdr_session_key()}")
    snapshot = HerdrClient().snapshot()
    print(f"herdr_version: {snapshot.get('version', 'unknown')}")
    print(f"herdr_protocol: {snapshot.get('protocol', 'unknown')}")
    agents = HerdrCLI(herdr_bin()).agent_list()
    print(f"herdr_agents: {len(agents)}")
    print(f"plugin_config: {config_dir()}")
    print(f"plugin_state: {data_dir()}")
    size = sum(
        path.stat().st_size
        for path in (
            db_path(),
            Path(str(db_path()) + "-wal"),
            Path(str(db_path()) + "-shm"),
        )
        if path.exists()
    )
    print(f"database_size_bytes: {size}")
    print(f"archive_indexing: {'enabled' if app_config()['archive_enabled'] else 'disabled'}")
    print(f"fzf: {shutil.which('fzf') or 'missing'}")
    conn = connect()
    docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    archive_sessions = conn.execute("SELECT COUNT(*) FROM archive_sessions").fetchone()[0]
    archive_docs = conn.execute("SELECT COUNT(*) FROM archive_docs").fetchone()[0]
    last = conn.execute("SELECT value FROM meta WHERE key = 'last_indexed_at'").fetchone()
    archive_last = conn.execute("SELECT value FROM meta WHERE key = 'last_archive_indexed_at'").fetchone()
    conn.close()
    print(f"indexed_chunks: {docs}")
    if last:
        print(f"last_indexed_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(last[0])))}")
    print(f"archive_sessions: {archive_sessions}")
    print(f"archive_chunks: {archive_docs}")
    if archive_last:
        print(f"last_archive_indexed_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(archive_last[0])))}")
    pid = read_watcher_pid()
    print(f"watcher: {'running ' + str(pid) if watcher_is_running() else 'stopped'}")
    return 0


def cmd_purge(args) -> int:
    if not args.yes:
        print("refusing to purge without --yes", file=sys.stderr)
        return 2
    cmd_watch_stop(args)
    for pid_file in sorted(data_dir().glob("watch*.pid")):
        stop_watcher_at(pid_file)
    for _ in range(40):
        if not any(lock_is_held(pid_file) for pid_file in data_dir().glob("watch*.pid")):
            break
        time.sleep(0.05)
    path = db_path()
    candidates = [path, Path(str(path) + "-wal"), Path(str(path) + "-shm")]
    for pattern in ("watch*.pid", "watch*.log", "index*.lock", "migrate.lock"):
        candidates.extend(sorted(data_dir().glob(pattern)))
    removed = 0
    for candidate in candidates:
        if lock_is_held(candidate):
            continue
        try:
            candidate.unlink()
            removed += 1
        except FileNotFoundError:
            pass
    print(f"purged OmniSearch index state ({removed} files); run index commands to rebuild")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-omnisearch")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("index", help="index current Herdr panes")
    p.add_argument("--lines", type=int, default=DEFAULT_LINES)
    p.add_argument("--include-empty", action="store_true")
    p.add_argument("--include-wrappers", action="store_true")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="search the index")
    p.add_argument("query", nargs="*", default=[])
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--status")
    p.add_argument("--agent")
    p.add_argument("--all-sessions", action="store_true", help="include rows from every Herdr session")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("archive-index", help="index persisted agent session logs")
    p.add_argument("--agents", default="", help="comma-separated agents to index")
    p.add_argument("--max-files", type=int)
    p.add_argument("--since-days", type=int)
    p.set_defaults(func=cmd_archive_index)

    p = sub.add_parser("archive-search", help="search persisted agent sessions")
    p.add_argument("query", nargs="*", default=[])
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    p.add_argument("--agent")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_archive_search)

    p = sub.add_parser("pick", help="search and focus selected Herdr target")
    p.add_argument("query", nargs="*", default=[])
    p.add_argument("--limit", type=int, default=80)
    p.add_argument("--lines", type=int, default=DEFAULT_LINES)
    p.add_argument("--status")
    p.add_argument("--agent")
    p.add_argument("--refresh", dest="refresh", action="store_true", default=True)
    p.add_argument("--no-refresh", dest="refresh", action="store_false")
    p.add_argument("--background-refresh", action="store_true")
    p.add_argument("--stale-seconds", type=int, default=10)
    p.add_argument("--include-empty", action="store_true")
    p.add_argument("--include-wrappers", action="store_true")
    p.add_argument("--all-sessions", action="store_true", help="include rows from every Herdr session")
    picker = p.add_mutually_exclusive_group()
    picker.add_argument("--native", action="store_true", help="force the native terminal picker")
    picker.add_argument("--fzf", action="store_true", help="use fzf instead of the native picker")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=pick)

    p = sub.add_parser("archive-pick", help="search old sessions and focus or resume selected agent")
    p.add_argument("query", nargs="*", default=[])
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--agent")
    p.add_argument("--agents", default="")
    p.add_argument("--max-files", type=int)
    p.add_argument("--since-days", type=int)
    p.add_argument("--refresh", dest="refresh", action="store_true", default=False)
    p.add_argument("--no-refresh", dest="refresh", action="store_false")
    p.add_argument("--background-refresh", action="store_true")
    p.add_argument("--stale-seconds", type=int, default=3600)
    picker = p.add_mutually_exclusive_group()
    picker.add_argument("--native", action="store_true", help="force the native terminal picker")
    picker.add_argument("--fzf", action="store_true", help="use fzf instead of the native picker")
    p.add_argument("--verbose", action="store_true")
    p.set_defaults(func=archive_pick)

    p = sub.add_parser("focus", help="focus a stable search result id")
    p.add_argument("stable_id")
    p.set_defaults(func=cmd_focus)

    p = sub.add_parser("archive-resume", help="resume an archived session result in Herdr")
    p.add_argument("stable_id")
    p.set_defaults(func=cmd_archive_resume)

    p = sub.add_parser("open-plugin-pane", help="open a managed OmniSearch plugin pane")
    p.add_argument("entrypoint", choices=("live", "archive"))
    p.set_defaults(func=cmd_open_plugin_pane)

    p = sub.add_parser("watch", help="keep the live index current from Herdr events")
    p.add_argument("--lines", type=int, default=DEFAULT_LINES)
    p.add_argument("--debounce", type=float, default=5.0)
    p.set_defaults(func=cmd_watch)

    p = sub.add_parser("watch-start", help="start the live index watcher")
    p.add_argument("--lines", type=int, default=DEFAULT_LINES)
    p.add_argument("--debounce", type=float, default=5.0)
    p.set_defaults(func=cmd_watch_start)

    p = sub.add_parser("watch-stop", help="stop the live index watcher")
    p.set_defaults(func=cmd_watch_stop)

    p = sub.add_parser("watch-status", help="show live index watcher state")
    p.set_defaults(func=cmd_watch_status)

    p = sub.add_parser("event-refresh", help=argparse.SUPPRESS)
    p.set_defaults(func=cmd_event_refresh)

    p = sub.add_parser("preview", help=argparse.SUPPRESS)
    p.add_argument("stable_id")
    p.set_defaults(func=cmd_preview)

    p = sub.add_parser("doctor", help="show OmniSearch state")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("purge", help="remove the local OmniSearch index")
    p.add_argument("--yes", action="store_true", help="confirm permanent index removal")
    p.set_defaults(func=cmd_purge)

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"herdr-omnisearch: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
