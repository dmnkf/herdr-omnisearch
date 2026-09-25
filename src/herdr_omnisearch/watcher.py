import os
import sqlite3
import sys
import time

from .herdr_cli import HerdrCLIError
from .herdr_socket import HerdrClient, HerdrError, HerdrTimeout
from .settings import data_dir
from .storage import (
    lock_is_held,
    read_watcher_pid,
    stop_watcher_at,
    try_exclusive_lock,
    watcher_pid_path,
)
from .live_index import index_session
from .machines import machines_config, maybe_background_sync

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
    sync_due = 0.0
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
                        if now >= sync_due:
                            sync_seconds = machines_config()["sync_seconds"]
                            maybe_background_sync(sync_seconds, discover=True)
                            sync_due = now + sync_seconds
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
