"""Merge the live indexes of Herdr's saved SSH machines into the local index.

Every machine keeps indexing its own panes with its own config. The machine
running the Herdr client pulls each remote's exported rows over SSH and stores
them under a machine namespace, so one picker can search and focus them all.
"""

import base64
import json
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor

from .herdr_cli import HerdrCLI, HerdrCLIError
from .live_index import replace_docs
from .settings import app_config, cli_command, data_dir, herdr_bin
from .storage import connect, spawn_locked_background, try_exclusive_lock
from .textmatch import tokens

EXPORT_FORMAT = 1

# Without synced rows, only the watcher looks for newly saved machines, and at
# this slow cadence, so single-machine setups stay free of extra processes.
DISCOVERY_SECONDS = 300

# Bounds what gets parsed; capture_output has already buffered the output.
MAX_EXPORT_CHARS = 64 * 1024 * 1024

TEXT_FIELDS = (
    "herdr_session", "socket_path", "workspace_id", "workspace_label", "tab_id",
    "terminal_id", "pane_id", "pane_label", "agent", "agent_session_id",
    "agent_status", "cwd", "foreground_cwd", "content", "body",
)

# Runs on the remote with only python3 required: locate the installed plugin
# from Herdr's registry instead of trusting a PATH that non-interactive SSH
# shells usually lack.
REMOTE_BOOTSTRAP = r"""
import json, os, shutil, sys
home = os.path.expanduser("~")
root = os.environ.get("HERDR_OMNISEARCH_ROOT")
if not root:
    registry = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "herdr", "plugins.json")
    try:
        with open(registry) as fh:
            root = next(p["plugin_root"] for p in json.load(fh) if p.get("plugin_id") == "herdr.omnisearch")
    except (OSError, ValueError, StopIteration, KeyError, TypeError):
        sys.exit("herdr.omnisearch is not installed on this machine")
local_bin = os.path.join(home, ".local", "bin")
if not shutil.which("herdr") and os.path.exists(os.path.join(local_bin, "herdr")):
    os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
os.environ["HERDR_PLUGIN_ROOT"] = root
sys.path.insert(0, os.path.join(root, "src"))
from herdr_omnisearch.cli import main
sys.exit(main(sys.argv[1:]))
"""


class MachineError(RuntimeError):
    pass


def machines_config():
    return app_config()["machines"]


def saved_machines():
    """Enabled saved SSH machines from `herdr machine list`, minus excluded labels.

    Raises MachineError when Herdr cannot answer, so callers never mistake a
    transient failure for "every machine was removed".
    """
    cfg = machines_config()
    if not cfg["enabled"]:
        return []
    try:
        profiles = HerdrCLI(herdr_bin()).machine_list()
    except HerdrCLIError as exc:
        raise MachineError(f"cannot list saved machines: {exc}") from exc
    excluded = {label.lower() for label in cfg["exclude"]}
    machines = []
    for profile in profiles:
        if not isinstance(profile, dict) or not profile.get("enabled", True):
            continue
        if not profile.get("id") or not profile.get("target"):
            continue
        label = profile.get("label") or profile["target"]
        if label.lower() in excluded or profile["target"].lower() in excluded:
            continue
        machines.append(
            {
                "id": profile["id"],
                "label": label,
                "target": profile["target"],
                "session": profile.get("session") or "default",
            }
        )
    return machines


def saved_machines_or_empty():
    try:
        return saved_machines()
    except MachineError:
        return []


def remote_argv(machine, args):
    cfg = machines_config()
    # A single-line command survives every login shell, csh included.
    encoded = base64.b64encode(REMOTE_BOOTSTRAP.encode("utf-8")).decode("ascii")
    remote = ["python3", "-c", f"import base64;exec(base64.b64decode('{encoded}'))", *args]
    if machine.get("session") and machine["session"] != "default":
        remote = ["env", f"HERDR_SESSION={machine['session']}", *remote]
    return [
        *shlex.split(cfg["ssh"]),
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={cfg['connect_timeout_seconds']}",
        "-o", "LogLevel=ERROR",
        "-o", "Compression=yes",
        "--",
        machine["target"],
        shlex.join(remote),
    ]


def run_remote(machine, args, *, timeout=None):
    timeout = timeout or machines_config()["timeout_seconds"]
    try:
        result = subprocess.run(
            remote_argv(machine, args),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MachineError(f"{machine['label']}: timed out after {timeout}s") from exc
    except OSError as exc:
        raise MachineError(f"{machine['label']}: cannot run ssh: {exc}") from exc
    if result.returncode != 0:
        if "invalid choice:" in (result.stderr or ""):
            raise MachineError(
                f"{machine['label']}: remote OmniSearch is older than 0.8.0; update it on that machine"
            )
        detail = (result.stderr or result.stdout or "unknown error").strip().splitlines()
        message = detail[-1] if detail else "unknown error"
        raise MachineError(f"{machine['label']}: {message[:300]}")
    return result.stdout


def fetch_export(machine):
    output = run_remote(machine, ["export"])
    if len(output) > MAX_EXPORT_CHARS:
        raise MachineError(f"{machine['label']}: export exceeds {MAX_EXPORT_CHARS} characters")
    try:
        payload = json.loads(output)
    except ValueError as exc:
        raise MachineError(f"{machine['label']}: export returned invalid JSON") from exc
    if not isinstance(payload, dict) or payload.get("format") != EXPORT_FORMAT:
        raise MachineError(f"{machine['label']}: unsupported export format")
    if not isinstance(payload.get("docs"), list):
        raise MachineError(f"{machine['label']}: export has no docs list")
    return payload


def as_int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def clean_doc(doc):
    """Coerce one exported row to the local schema, or None when it is unusable."""
    if not isinstance(doc, dict):
        return None
    cleaned = {}
    for field in TEXT_FIELDS:
        value = doc.get(field)
        cleaned[field] = value if isinstance(value, str) else ""
    if not doc.get("stable_id") or not isinstance(doc["stable_id"], str) or not cleaned["pane_id"]:
        return None
    cleaned["stable_id"] = doc["stable_id"]
    cleaned["chunk_index"] = as_int(doc.get("chunk_index"))
    cleaned["indexed_at"] = as_int(doc.get("indexed_at"))
    cleaned["body"] = cleaned["body"] or cleaned["content"]
    return cleaned


def ingest_export(conn, machine, payload) -> int:
    prefix = machine["id"][:12]
    docs = []
    doc_tokens = []
    for raw in payload["docs"]:
        doc = clean_doc(raw)
        if doc is None:
            continue
        stable_id = f"{prefix}:{doc['stable_id']}"
        doc.update(
            {
                "stable_id": stable_id,
                "herdr_session": f"{machine['label']}/{doc['herdr_session'] or 'default'}",
                "machine_id": machine["id"],
                "machine_label": machine["label"],
            }
        )
        docs.append(doc)
        doc_tokens.extend((token, stable_id) for token in set(tokens(doc["body"])))
    replace_docs(conn, "machine_id = :machine", {"machine": machine["id"]}, docs, doc_tokens)
    return len(docs)


def machine_status(conn, machine_id):
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (f"machine:{machine_id}",)).fetchone()
    try:
        return json.loads(row[0]) if row else {}
    except ValueError:
        return {}


def write_status(conn, machine, status) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (f"machine:{machine['id']}", json.dumps({**status, "label": machine["label"]})),
    )


def failed_status(conn, machine, error, now):
    previous = machine_status(conn, machine["id"])
    return {
        "ok": False,
        "error": str(error),
        "synced_at": previous.get("synced_at", 0),
        "docs": previous.get("docs", 0),
        "attempted_at": now,
    }


def sync_machines():
    """Pull every saved machine concurrently; a failing machine keeps its last rows."""
    machines = saved_machines()
    results = {}
    if machines:
        with ThreadPoolExecutor(max_workers=min(8, len(machines))) as pool:
            futures = {machine["id"]: pool.submit(fetch_export, machine) for machine in machines}
            for machine in machines:
                try:
                    results[machine["id"]] = futures[machine["id"]].result()
                except MachineError as exc:
                    results[machine["id"]] = exc

    now = int(time.time())
    summary = []
    conn = connect()
    try:
        with conn:
            known = {machine["id"] for machine in machines}
            for (machine_id,) in conn.execute(
                "SELECT DISTINCT machine_id FROM docs WHERE machine_id <> ''"
            ).fetchall():
                if machine_id not in known:
                    replace_docs(conn, "machine_id = :machine", {"machine": machine_id}, [], [])
                    conn.execute("DELETE FROM meta WHERE key = ?", (f"machine:{machine_id}",))
            for machine in machines:
                result = results[machine["id"]]
                if isinstance(result, MachineError):
                    status = failed_status(conn, machine, result, now)
                else:
                    # A bad payload from one machine must not roll back the others.
                    conn.execute("SAVEPOINT machine_ingest")
                    try:
                        count = ingest_export(conn, machine, result)
                    except Exception as exc:
                        conn.execute("ROLLBACK TO machine_ingest")
                        conn.execute("RELEASE machine_ingest")
                        status = failed_status(conn, machine, f"{machine['label']}: bad export: {exc}", now)
                    else:
                        conn.execute("RELEASE machine_ingest")
                        remote_error = result.get("error")
                        status = {
                            "ok": True,
                            "error": remote_error if isinstance(remote_error, str) else "",
                            "synced_at": now,
                            "docs": count,
                            "attempted_at": now,
                            "remote_version": str(result.get("version") or ""),
                            "hostname": str(result.get("hostname") or ""),
                        }
                write_status(conn, machine, status)
                summary.append((machine, status))
            if machines:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('machines_synced_at', ?)",
                    (str(now),),
                )
            else:
                conn.execute("DELETE FROM meta WHERE key = 'machines_synced_at'")
    finally:
        conn.close()
    return summary


def machine_statuses():
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'machine:%'").fetchall()
    finally:
        conn.close()
    statuses = {}
    for key, value in rows:
        try:
            statuses[key.split(":", 1)[1]] = json.loads(value)
        except ValueError:
            continue
    return statuses


def sync_lock_path():
    return data_dir() / "machines-sync.lock"


def read_meta_int(conn, key) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return as_int(row[0]) if row else 0


def maybe_background_sync(stale_seconds: int, *, discover=False) -> None:
    """Refresh synced machines when stale; only `discover` callers look for new ones."""
    if not machines_config()["enabled"]:
        return
    now = int(time.time())
    conn = connect()
    try:
        synced_at = read_meta_int(conn, "machines_synced_at")
        if synced_at:
            if now - synced_at < stale_seconds:
                return
        else:
            if not discover or now - read_meta_int(conn, "machines_checked_at") < DISCOVERY_SECONDS:
                return
            with conn:
                conn.execute(
                    "INSERT OR REPLACE INTO meta (key, value) VALUES ('machines_checked_at', ?)",
                    (str(now),),
                )
    finally:
        conn.close()
    if not synced_at and not saved_machines_or_empty():
        return
    lock_fd = try_exclusive_lock(sync_lock_path())
    if lock_fd is None:
        return
    spawn_locked_background([*cli_command(), "sync-machines", "--locked"], lock_fd)


def machine_for_row(row):
    for machine in saved_machines():
        if machine["id"] == row.get("machine_id"):
            return machine
    raise MachineError(f"machine {row.get('machine_label') or row.get('machine_id')} is no longer saved")


def focus_remote_row(row) -> None:
    # Target ids, not the stable id: stable ids change with every content refresh.
    args = ["focus-target", "--workspace-id", row.get("workspace_id") or ""]
    if row.get("tab_id"):
        args += ["--tab-id", row["tab_id"]]
    if (row.get("pane_id") or "").startswith("workspace:"):
        args += ["--workspace-only"]
    elif row.get("pane_id"):
        args += ["--pane-id", row["pane_id"]]
        if row.get("agent"):
            args += ["--agent", row["agent"]]
    run_remote(machine_for_row(row), args)
