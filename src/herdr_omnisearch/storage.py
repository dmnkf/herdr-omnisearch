import errno
import fcntl
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .settings import data_dir, ensure_private_directory, herdr_session_key, legacy_data_dir

ARCHIVE_CATALOG_SCHEMA_READY = set()


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


def archive_catalog_db_path() -> Path:
    override = os.environ.get("HERDR_OMNISEARCH_CATALOG_DB")
    if override:
        return Path(override)
    return data_dir() / "archive-catalog.sqlite3"


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
            if "docs" in tables and conn.execute("SELECT 1 FROM docs LIMIT 1").fetchone():
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
        """
    )
    ensure_column(conn, "docs", "agent_session_id", "TEXT")
    ensure_column(conn, "docs", "herdr_session", "TEXT")
    ensure_column(conn, "docs", "socket_path", "TEXT")
    ensure_column(conn, "docs", "machine_id", "TEXT NOT NULL DEFAULT ''")
    ensure_column(conn, "docs", "machine_label", "TEXT")
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_docs_agent_session_id ON docs(agent_session_id);
        CREATE INDEX IF NOT EXISTS idx_docs_herdr_session ON docs(herdr_session);
        CREATE INDEX IF NOT EXISTS idx_docs_machine_id ON docs(machine_id);
        """
    )


COMPACT_MIN_FREE_BYTES = 16 * 1024 * 1024


def index_is_bloated(conn) -> bool:
    """Tables left by the archive index removed in 0.7.0, or a mostly stale vocabulary."""
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'archive_token_docs'").fetchone():
        return True
    total = conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0]
    referenced = conn.execute("SELECT COUNT(DISTINCT token) FROM token_docs").fetchone()[0]
    return total > 4 * referenced + 5000


def reset_index_file() -> None:
    path = db_path()
    for suffix in ("", "-wal", "-shm"):
        try:
            Path(str(path) + suffix).unlink()
        except FileNotFoundError:
            pass


def compact_index(conn) -> None:
    """Reclaim free pages once they pile up; a busy database is left for next time."""
    try:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        free = conn.execute("PRAGMA freelist_count").fetchone()[0]
        if free * page_size >= COMPACT_MIN_FREE_BYTES and free * 4 >= pages:
            conn.execute("VACUUM")
            # In WAL mode the smaller file only lands once the log is checkpointed.
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError:
        pass


def archive_catalog_connect():
    requested_path = archive_catalog_db_path().expanduser()
    existed = requested_path.exists()
    path = prepare_database_path(requested_path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=500")
    conn.execute("PRAGMA synchronous=NORMAL")
    cache_key = str(path.absolute())
    if not existed:
        ARCHIVE_CATALOG_SCHEMA_READY.discard(cache_key)
    if cache_key in ARCHIVE_CATALOG_SCHEMA_READY:
        return conn
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS catalog_sessions (
            session_key TEXT PRIMARY KEY,
            agent TEXT NOT NULL,
            session_id TEXT NOT NULL,
            space_label TEXT,
            title TEXT,
            cwd TEXT,
            path TEXT NOT NULL UNIQUE,
            started_at TEXT,
            updated_at TEXT,
            started_epoch INTEGER NOT NULL DEFAULT 0,
            updated_epoch INTEGER NOT NULL DEFAULT 0,
            preview TEXT,
            is_wrapper INTEGER NOT NULL DEFAULT 0,
            source_size INTEGER NOT NULL,
            source_mtime_ns INTEGER NOT NULL,
            indexed_at INTEGER NOT NULL,
            message_generation INTEGER NOT NULL DEFAULT 0,
            message_count INTEGER NOT NULL DEFAULT 0,
            message_index_version INTEGER NOT NULL DEFAULT 0,
            is_present INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE IF NOT EXISTS catalog_messages (
            id INTEGER PRIMARY KEY,
            message_key TEXT NOT NULL UNIQUE,
            session_key TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            role TEXT NOT NULL,
            message_at TEXT,
            message_epoch INTEGER NOT NULL DEFAULT 0,
            content TEXT NOT NULL,
            indexed_generation INTEGER NOT NULL
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS catalog_message_fts USING fts5(
            content,
            content = 'catalog_messages',
            content_rowid = 'id',
            tokenize = 'unicode61'
        );

        CREATE VIRTUAL TABLE IF NOT EXISTS catalog_message_vocab USING fts5vocab(
            catalog_message_fts,
            'row'
        );

        CREATE TABLE IF NOT EXISTS catalog_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_catalog_sessions_agent ON catalog_sessions(agent);
        CREATE INDEX IF NOT EXISTS idx_catalog_sessions_started_epoch ON catalog_sessions(started_epoch);
        CREATE INDEX IF NOT EXISTS idx_catalog_messages_session_generation_ordinal
            ON catalog_messages(session_key, indexed_generation, ordinal);
        """
    )
    ensure_column(conn, "catalog_sessions", "space_label", "TEXT")
    ensure_column(
        conn,
        "catalog_sessions",
        "space_label_source",
        "TEXT NOT NULL DEFAULT 'derived'",
    )
    ensure_column(conn, "catalog_sessions", "started_epoch", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "catalog_sessions", "updated_epoch", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "catalog_sessions", "message_generation", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "catalog_sessions", "message_count", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "catalog_sessions", "message_index_version", "INTEGER NOT NULL DEFAULT 0")
    ensure_column(conn, "catalog_sessions", "is_present", "INTEGER NOT NULL DEFAULT 1")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_catalog_sessions_started_epoch ON catalog_sessions(started_epoch)"
    )
    ARCHIVE_CATALOG_SCHEMA_READY.add(cache_key)
    return conn


def ensure_column(conn, table: str, column: str, definition: str):
    columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


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


def exclusive_lock(path: Path):
    """Take a blocking exclusive flock and return its owning descriptor."""
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode("utf-8"))
        return fd
    except BaseException:
        os.close(fd)
        raise


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


def spawn_locked_background(cmd, lock_fd, *, lock_env=None) -> None:
    # The child inherits lock_fd, so the flock is held for its whole
    # lifetime and releases automatically when it exits or crashes.
    env = os.environ.copy()
    if lock_env:
        env[lock_env] = str(lock_fd)
    try:
        subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            pass_fds=(lock_fd,),
        )
    finally:
        os.close(lock_fd)


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
