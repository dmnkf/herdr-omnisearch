#!/usr/bin/env python3

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .herdr_cli import HerdrCLI
from .herdr_socket import HerdrClient, resolve_socket_path
from .settings import (
    DEFAULT_LIMIT,
    DEFAULT_LINES,
    app_config,
    cli_command,
    config_dir,
    data_dir,
    herdr_bin,
    herdr_session_key,
)
from .storage import (
    archive_catalog_db_path,
    connect,
    db_path,
    lock_is_held,
    read_watcher_pid,
    release_index_lock,
    stop_watcher_at,
    watcher_log_path,
)
from .textmatch import clean_text
from .live_index import grouped_search_index, index_session, maybe_background_index
from .archive_catalog import (
    archive_catalog_index,
    archive_catalog_result,
    archive_catalog_search,
    archive_catalog_state,
    archive_catalog_state_name,
    mark_archive_row,
    maybe_background_archive_catalog_index,
)
from .render import display_status, format_result
from .navigate import focus_archive_catalog_result, focus_result
from .picker import archive_pick, pick
from .watcher import watch_live_index, watcher_is_running

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


def cmd_archive_catalog_index(args) -> int:
    changed, unchanged, removed, message_count = archive_catalog_index(args.agents)
    print(
        f"cataloged {changed} changed / {unchanged} unchanged / {removed} removed "
        f"sessions ({message_count} messages) into {archive_catalog_db_path()}"
    )
    return 0


def cmd_archive_catalog_start(args) -> int:
    if not app_config()["archive_enabled"]:
        print("archive catalog: disabled")
        return 0
    maybe_background_archive_catalog_index(args.agents, args.stale_seconds)
    count, last = archive_catalog_state()
    state = archive_catalog_state_name(count, last)
    print(f"archive catalog: {state} ({count} sessions, last indexed {last or 'never'})")
    return 0


def cmd_archive_catalog_status(_args) -> int:
    count, last = archive_catalog_state()
    state = archive_catalog_state_name(count, last)
    print(f"archive catalog: {state}")
    print(f"archive catalog sessions: {count}")
    if last:
        print(
            "archive catalog last indexed: "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last))
        )
    return 0


def cmd_archive_search(args) -> int:
    rows = archive_catalog_search(" ".join(args.query), args.limit, agent=args.agent)
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
    return focus_archive_catalog_result(args.stable_id)


def cmd_preview(args) -> int:
    if args.stable_id.startswith("archive-catalog:"):
        row = archive_catalog_result(args.stable_id)
    else:
        conn = connect()
        row = conn.execute("SELECT * FROM docs WHERE stable_id = ?", (args.stable_id,)).fetchone()
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
    print(f"archive_catalog_db: {archive_catalog_db_path()}")
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
    catalog_size = sum(
        path.stat().st_size
        for path in (
            archive_catalog_db_path(),
            Path(str(archive_catalog_db_path()) + "-wal"),
            Path(str(archive_catalog_db_path()) + "-shm"),
        )
        if path.exists()
    )
    print(f"archive_catalog_size_bytes: {catalog_size}")
    print(f"archive_indexing: {'enabled' if app_config()['archive_enabled'] else 'disabled'}")
    print(f"fzf: {shutil.which('fzf') or 'missing'}")
    conn = connect()
    docs = conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    last = conn.execute("SELECT value FROM meta WHERE key = 'last_indexed_at'").fetchone()
    conn.close()
    print(f"indexed_chunks: {docs}")
    if last:
        print(f"last_indexed_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(int(last[0])))}")
    catalog_count, catalog_last = archive_catalog_state()
    print(f"archive_catalog_sessions: {catalog_count}")
    print(f"archive_catalog: {archive_catalog_state_name(catalog_count, catalog_last)}")
    if catalog_last:
        print(
            "last_archive_catalog_indexed_at: "
            + time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(catalog_last))
        )
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
    catalog_path = archive_catalog_db_path()
    candidates = [
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        catalog_path,
        Path(str(catalog_path) + "-wal"),
        Path(str(catalog_path) + "-shm"),
        data_dir() / "archive-catalog.lock",
    ]
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

    p = sub.add_parser(
        "archive-catalog-index",
        help="incrementally catalog all persisted agent sessions",
    )
    p.add_argument("--agents", default="", help="comma-separated agents to catalog")
    p.set_defaults(func=cmd_archive_catalog_index)

    p = sub.add_parser("archive-catalog-start", help="refresh the archive catalog in the background")
    p.add_argument("--agents", default="", help="comma-separated agents to catalog")
    p.add_argument("--stale-seconds", type=int, default=300)
    p.set_defaults(func=cmd_archive_catalog_start)

    p = sub.add_parser("archive-catalog-status", help="show archive catalog state")
    p.set_defaults(func=cmd_archive_catalog_status)

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
    p.add_argument("--window-days", type=int, help="days held in the active archive window")
    p.add_argument("--window-offset", type=int, default=0, help="windows before the newest window")
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
