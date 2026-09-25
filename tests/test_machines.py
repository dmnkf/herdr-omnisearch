import io
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))
os.environ.setdefault("HERDR_PLUGIN_STATE_DIR", tempfile.mkdtemp(prefix="herdr-omnisearch-tests-"))

from herdr_omnisearch import cli, live_index, machines, picker, settings, storage  # noqa: E402
from herdr_omnisearch.herdr_cli import HerdrCLIError  # noqa: E402


INTRANET = {"id": "a" * 32, "label": "intranet", "target": "d-intranet01", "session": "default", "selected": False}
PERSONAL = {"id": "b" * 32, "label": "personal", "target": "d-personal01", "session": "default", "selected": False}


def export_payload(workspace_label, text, *, panes=1):
    # Every remote reports the same session key and ids, as two hosts running
    # Herdr as root with default sockets really do.
    docs = [{
        "stable_id": "ws-w1",
        "herdr_session": "default-2cd4ce75",
        "socket_path": "/root/.config/herdr/herdr.sock",
        "workspace_id": "w1",
        "workspace_label": workspace_label,
        "tab_id": "w1:t1",
        "pane_id": "workspace:w1",
        "pane_label": workspace_label,
        "agent": "",
        "agent_status": "workspace",
        "chunk_index": 0,
        "content": f"type workspace\nworkspace {workspace_label}",
        "body": f"type workspace\nworkspace {workspace_label}",
        "indexed_at": 100,
    }]
    for index in range(panes):
        docs.append({
            "stable_id": f"pane-{index}",
            "herdr_session": "default-2cd4ce75",
            "socket_path": "/root/.config/herdr/herdr.sock",
            "workspace_id": "w1",
            "workspace_label": workspace_label,
            "tab_id": "w1:t1",
            "pane_id": f"w1:p{index}",
            "pane_label": "agent",
            "agent": "claude",
            "agent_status": "idle",
            "cwd": "/root/project",
            "chunk_index": 0,
            "content": text,
            "body": f"workspace {workspace_label}\nagent claude\n\n{text}",
            "indexed_at": 100,
        })
    return {"format": machines.EXPORT_FORMAT, "version": "0.8.0", "hostname": "host", "docs": docs}


class LocalHerdrClient:
    def snapshot(self):
        return {
            "workspaces": [{"workspace_id": "w1", "active_tab_id": "w1:t1", "label": "Laptop"}],
            "panes": [{
                "workspace_id": "w1",
                "tab_id": "w1:t1",
                "pane_id": "w1:p1",
                "label": "agent",
                "agent": "codex",
                "agent_status": "idle",
                "cwd": "/Users/me/project",
            }],
        }

    def pane_read(self, pane_id, lines):
        return "deploy notes from the laptop"


class LocalHerdrCLI:
    def __init__(self, binary="herdr", runner=None, env=None):
        pass

    def agent_list(self):
        return []


class MachineTests(unittest.TestCase):
    def setUp(self):
        settings.CONFIG_CACHE = None
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(
            os.environ,
            {
                "HERDR_OMNISEARCH_DB": str(Path(self.tmp.name) / "index.sqlite3"),
                "HERDR_PLUGIN_STATE_DIR": self.tmp.name,
                "HERDR_SOCKET_PATH": str(Path(self.tmp.name) / "herdr.sock"),
            },
        )
        env.start()
        self.addCleanup(env.stop)
        for target, fake in ((live_index, "HerdrClient"), (live_index, "HerdrCLI")):
            patcher = patch.object(target, fake, LocalHerdrClient if fake == "HerdrClient" else LocalHerdrCLI)
            patcher.start()
            self.addCleanup(patcher.stop)

    def sync(self, results, saved=(INTRANET, PERSONAL)):
        def fetch(machine):
            result = results[machine["label"]]
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(machines, "saved_machines", return_value=list(saved)), patch.object(
            machines, "fetch_export", side_effect=fetch
        ):
            return machines.sync_machines()

    def test_single_machine_results_are_unchanged(self):
        live_index.index_session(50, False, False)
        with patch.object(machines, "saved_machines", return_value=[]):
            self.assertEqual(machines.sync_machines(), [])
        args = Namespace(limit=20, agent=None, status=None, all_sessions=False, local_only=False)
        for query in ("", "deploy"):
            rows = picker.picker_rows(args, query)
            self.assertEqual(
                [picker.row_title(row) for row in rows],
                ["[workspace] Laptop", "  [idle] codex / agent"],
            )
            self.assertEqual(picker.row_title(rows[1], full=True), "  [idle] Laptop / codex / agent")
            self.assertEqual(rows, live_index.grouped_search_index(query, 20, machines=False))

    def test_single_machine_picker_and_watcher_spawn_no_herdr_calls(self):
        with patch.object(machines.HerdrCLI, "machine_list", return_value=[]) as listing, patch.object(
            machines, "spawn_locked_background"
        ) as spawn:
            for _ in range(3):
                machines.maybe_background_sync(10)
            listing.assert_not_called()
            # The watcher discovers new machines, but only once per DISCOVERY_SECONDS.
            machines.maybe_background_sync(10, discover=True)
            machines.maybe_background_sync(10, discover=True)
            self.assertEqual(listing.call_count, 1)
        spawn.assert_not_called()

    def test_failed_machine_listing_keeps_synced_rows(self):
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        with patch.object(machines.HerdrCLI, "machine_list", side_effect=HerdrCLIError("protocol mismatch")):
            with self.assertRaises(machines.MachineError):
                machines.sync_machines()
        self.assertEqual(len(live_index.search_index("remote", 20, machines=True)), 2)
        self.assertEqual(len(machines.machine_statuses()), 2)

    def test_bad_export_only_fails_its_own_machine(self):
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        broken = export_payload("Broken", "remote text")
        broken["docs"][1]["body"] = ["not", "text"]
        broken["docs"][1]["chunk_index"] = "x"
        broken["docs"].append("garbage")
        summary = dict(
            (machine["label"], status)
            for machine, status in self.sync({
                "intranet": export_payload("Billing2", "fresh text"),
                "personal": broken,
            })
        )
        self.assertTrue(summary["intranet"]["ok"])
        self.assertTrue(live_index.search_index("machine:intranet fresh", 20, machines=True))
        self.assertTrue(summary["personal"]["ok"])
        self.assertEqual(summary["personal"]["docs"], 2)

        with patch.object(machines, "ingest_export", side_effect=[3, sqlite3.ProgrammingError("boom")]):
            summary = dict(
                (machine["label"], status)
                for machine, status in self.sync({
                    "intranet": export_payload("Billing3", "newest text"),
                    "personal": export_payload("Game", "remote text"),
                })
            )
        self.assertTrue(summary["intranet"]["ok"])
        self.assertFalse(summary["personal"]["ok"])
        self.assertIn("bad export", summary["personal"]["error"])
        self.assertTrue(live_index.search_index("machine:personal remote", 20, machines=True))

    def test_remote_index_failure_marks_the_machine_stale(self):
        payload = export_payload("Billing", "remote text")
        payload["error"] = "cannot connect to Herdr socket"
        summary = dict((machine["label"], status) for machine, status in self.sync({
            "intranet": payload,
            "personal": export_payload("Game", "remote text"),
        }))
        self.assertTrue(summary["intranet"]["ok"])
        self.assertEqual(summary["intranet"]["error"], "cannot connect to Herdr socket")
        self.assertEqual(cli.machine_state(summary["intranet"]), "stale")
        args = Namespace(limit=20, agent=None, status=None, all_sessions=False, local_only=False)
        headers = [row for row in picker.picker_rows(args, "") if live_index.is_machine_row(row)]
        intranet = next(row for row in headers if row["machine_label"] == "intranet")
        self.assertIn("stale", picker.row_title(intranet))

    def test_disabling_machines_hides_synced_rows(self):
        live_index.index_session(50, False, False)
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        config = settings.default_config()
        config["machines"]["enabled"] = False
        args = Namespace(limit=20, agent=None, status=None, all_sessions=False, local_only=False)
        with patch.object(settings, "CONFIG_CACHE", config):
            labels = {row.get("workspace_label") for row in picker.picker_rows(args, "")}
        self.assertEqual(labels, {"Laptop"})

    def test_fzf_input_leaves_out_machine_headers(self):
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        args = Namespace(query=[], limit=20, agent=None, status=None, all_sessions=False, local_only=False)
        with patch.object(picker.shutil, "which", return_value="/usr/bin/fzf"), patch.object(
            picker.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, stdout="")
        ) as run:
            picker.fzf_picker(args)
        lines = run.call_args.kwargs["input"].splitlines()
        self.assertTrue(lines)
        self.assertFalse(any(line.startswith("machine:") for line in lines))

    def test_remote_focus_targets_ids_not_stable_ids(self):
        row = {
            "machine_id": INTRANET["id"],
            "machine_label": "intranet",
            "workspace_id": "w4",
            "tab_id": "w4:t1",
            "pane_id": "w4:p2",
            "agent": "claude",
        }
        with patch.object(machines, "saved_machines", return_value=[INTRANET]), patch.object(
            machines, "run_remote"
        ) as run:
            machines.focus_remote_row(row)
            machines.focus_remote_row({**row, "pane_id": "workspace:w4", "agent": ""})
        self.assertEqual(
            run.call_args_list[0].args[1],
            ["focus-target", "--workspace-id", "w4", "--tab-id", "w4:t1", "--pane-id", "w4:p2", "--agent", "claude"],
        )
        self.assertEqual(
            run.call_args_list[1].args[1],
            ["focus-target", "--workspace-id", "w4", "--tab-id", "w4:t1", "--workspace-only"],
        )

    def test_machines_merge_without_id_collisions(self):
        live_index.index_session(50, False, False)
        self.sync({
            "intranet": export_payload("Billing", "deploy pipeline for billing"),
            "personal": export_payload("Game", "deploy the game server"),
        })

        rows = live_index.grouped_search_index("", 20, machines=True)
        headers = [row for row in rows if live_index.is_machine_row(row)]
        self.assertEqual([live_index.machine_name(row) for row in headers], ["Local", "intranet", "personal"])
        workspaces = [row["workspace_label"] for row in rows if live_index.is_workspace_row(row)]
        self.assertEqual(workspaces, ["Laptop", "Billing", "Game"])
        self.assertEqual({row["_tree_depth"] for row in rows if live_index.is_workspace_row(row)}, {1})

        conn = sqlite3.connect(os.environ["HERDR_OMNISEARCH_DB"])
        try:
            total, distinct = conn.execute("SELECT COUNT(*), COUNT(DISTINCT stable_id) FROM docs").fetchone()
        finally:
            conn.close()
        self.assertEqual(total, distinct)

    def test_query_pins_top_matches_across_machines_above_the_tree(self):
        live_index.index_session(50, False, False)
        self.sync({
            "intranet": export_payload("Billing", "deploy pipeline for billing"),
            "personal": export_payload("Game", "deploy the game server"),
        })
        rows = live_index.grouped_search_index("deploy", 20, machines=True)
        top = [row for row in rows if row.get("_top_match")]
        self.assertEqual(rows[: len(top)], top)
        self.assertEqual(
            {live_index.machine_name(row) for row in top},
            {"Local", "intranet", "personal"},
        )
        self.assertIn("intranet › Billing", picker.row_title(top[[live_index.machine_name(r) for r in top].index("intranet")]))

    def test_hits_on_one_machine_are_not_pinned_twice(self):
        self.sync({
            "intranet": export_payload("Billing", "deploy pipeline for billing"),
            "personal": export_payload("Game", "quiet output"),
        })
        rows = live_index.grouped_search_index("pipeline", 20, machines=True)
        self.assertFalse(any(row.get("_top_match") for row in rows))
        self.assertEqual([live_index.machine_name(r) for r in rows if live_index.is_machine_row(r)], ["intranet"])

    def test_path_column_keeps_the_distinguishing_tail(self):
        path = "/home/intranet/intranet/intranet-worktrees/20260902-dfi-expedia-recapture-precheck"
        fitted = picker.shorten_start(path, 40)
        self.assertEqual(len(fitted), 40)
        self.assertTrue(fitted.startswith("…") and fitted.endswith("expedia-recapture-precheck"))

    def test_busy_machine_cannot_crowd_out_the_others(self):
        self.sync({
            "intranet": export_payload("Billing", "busy output", panes=30),
            "personal": export_payload("Game", "quiet output"),
        })
        rows = live_index.grouped_search_index("", 5, machines=True)
        self.assertIn("personal", [live_index.machine_name(row) for row in rows if live_index.is_machine_row(row)])

    def test_local_indexing_and_reaping_keep_synced_rows(self):
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        # Synced socket paths do not exist here; the dead-session reaper must not treat them as local.
        live_index.index_session(50, False, False)
        live_index.index_session(50, False, False)
        rows = live_index.search_index("remote", 20, machines=True)
        self.assertEqual({row["machine_label"] for row in rows}, {"intranet", "personal"})
        self.assertFalse(live_index.search_index("remote", 20))

    def test_failed_sync_keeps_last_rows_and_forgotten_machines_are_dropped(self):
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        summary = self.sync({
            "intranet": machines.MachineError("intranet: timed out after 20s"),
            "personal": export_payload("Game", "remote text"),
        })
        status = dict((machine["label"], state) for machine, state in summary)
        self.assertFalse(status["intranet"]["ok"])
        self.assertEqual(status["intranet"]["docs"], 2)
        self.assertTrue(live_index.search_index("machine:intranet remote", 20, machines=True))

        self.sync({"personal": export_payload("Game", "remote text")}, saved=(PERSONAL,))
        self.assertFalse(live_index.search_index("machine:intranet remote", 20, machines=True))
        self.assertNotIn(INTRANET["id"], machines.machine_statuses())

    def test_export_only_contains_this_sessions_own_rows(self):
        live_index.index_session(50, False, False)
        self.sync({
            "intranet": export_payload("Billing", "remote text"),
            "personal": export_payload("Game", "remote text"),
        })
        out = io.StringIO()
        with patch.object(cli, "watcher_is_running", return_value=True), patch.object(
            cli, "socket_is_alive", return_value=True
        ), redirect_stdout(out):
            self.assertEqual(cli.cmd_export(None), 0)
        payload = json.loads(out.getvalue())
        self.assertEqual(payload["format"], machines.EXPORT_FORMAT)
        self.assertEqual({doc["workspace_label"] for doc in payload["docs"]}, {"Laptop"})
        self.assertTrue(all(doc["body"] for doc in payload["docs"]))
        self.assertTrue(all("machine_id" not in doc for doc in payload["docs"]))
        self.assertEqual(payload["error"], "")

    def test_export_reports_a_dead_herdr_server_even_with_a_watcher(self):
        live_index.index_session(50, False, False)
        out = io.StringIO()
        with patch.object(cli, "watcher_is_running", return_value=True), redirect_stdout(out):
            self.assertEqual(cli.cmd_export(None), 0)
        payload = json.loads(out.getvalue())
        self.assertIn("not running", payload["error"])
        self.assertTrue(payload["docs"])

    def test_saved_machines_come_from_herdr_and_respect_config(self):
        profiles = [
            {**INTRANET, "enabled": True},
            {**PERSONAL, "enabled": True},
            {"id": "c" * 32, "label": "old", "target": "old-box", "session": "default", "enabled": False},
        ]
        config = settings.default_config()
        config["machines"]["exclude"] = ["personal"]
        with patch.object(settings, "CONFIG_CACHE", config), patch.object(
            machines.HerdrCLI, "_run", return_value=json.dumps(profiles)
        ):
            self.assertEqual([machine["label"] for machine in machines.saved_machines()], ["intranet"])
        with patch.object(machines.HerdrCLI, "_run", side_effect=HerdrCLIError("unknown command: machine")):
            with self.assertRaises(machines.MachineError):
                machines.saved_machines()
            self.assertEqual(machines.saved_machines_or_empty(), [])
        config["machines"]["enabled"] = False
        with patch.object(settings, "CONFIG_CACHE", config), patch.object(machines.HerdrCLI, "_run") as run:
            self.assertEqual(machines.saved_machines(), [])
        run.assert_not_called()

    def test_remote_command_is_non_interactive_and_targets_the_saved_session(self):
        argv = machines.remote_argv({**PERSONAL, "session": "agents"}, ["export"])
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertEqual(argv[-3:-1], ["--", "d-personal01"])
        self.assertNotIn("\n", argv[-1])
        remote = shlex.split(argv[-1])
        self.assertEqual(remote[:2], ["env", "HERDR_SESSION=agents"])
        self.assertEqual(remote[-1], "export")

    def test_remote_bootstrap_finds_the_plugin_through_herdrs_registry(self):
        home = Path(self.tmp.name) / "home"
        (home / ".config" / "herdr").mkdir(parents=True)
        (home / ".config" / "herdr" / "plugins.json").write_text(
            json.dumps([{"plugin_id": "herdr.omnisearch", "plugin_root": str(ROOT)}]),
            encoding="utf-8",
        )
        remote = machines.remote_argv(INTRANET, ["export"])[-1]
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("HERDR_PLUGIN", "PYTHONPATH", "XDG_"))
        }
        env.update({"HOME": str(home), "HERDR_OMNISEARCH_DB": os.environ["HERDR_OMNISEARCH_DB"]})
        result = subprocess.run(["sh", "-c", remote], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["format"], machines.EXPORT_FORMAT)
        # No Herdr server is reachable here: export reports it instead of failing the sync.
        self.assertTrue(payload["error"])


if __name__ == "__main__":
    unittest.main()
