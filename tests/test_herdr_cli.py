import json
import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from herdr_omnisearch.herdr_cli import HerdrCLI, HerdrCLIError  # noqa: E402


class FakeRunner:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        return self.responses.pop(0)


def completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr=stderr)


class HerdrCLITests(unittest.TestCase):
    def test_agent_list_parses_result_envelope(self):
        runner = FakeRunner(
            [completed(json.dumps({"result": {"agents": [{"pane_id": "w1:p1"}]}}))]
        )
        client = HerdrCLI("/opt/herdr", runner=runner)

        self.assertEqual(client.agent_list(), [{"pane_id": "w1:p1"}])
        self.assertEqual(runner.calls[0][0], ["/opt/herdr", "agent", "list"])

    def test_agent_read_uses_stable_unwrapped_text_contract(self):
        runner = FakeRunner([completed("terminal text\n")])
        client = HerdrCLI(runner=runner)

        self.assertEqual(client.agent_read("w1:p1", 250), "terminal text\n")
        self.assertEqual(
            runner.calls[0][0],
            [
                "herdr",
                "agent",
                "read",
                "w1:p1",
                "--source",
                "recent-unwrapped",
                "--lines",
                "250",
                "--format",
                "text",
            ],
        )

    def test_agent_start_passes_resume_arguments_after_separator(self):
        runner = FakeRunner([completed('{"result":{"type":"agent_start"}}')])
        client = HerdrCLI(runner=runner)

        client.agent_start(
            "resume-codex-019abc",
            "codex",
            "w1:p1",
            ["resume", "-C", "/tmp/project", "019abc"],
            timeout_ms=45000,
        )

        self.assertEqual(
            runner.calls[0][0],
            [
                "herdr",
                "agent",
                "start",
                "resume-codex-019abc",
                "--kind",
                "codex",
                "--pane",
                "w1:p1",
                "--timeout",
                "45000",
                "--",
                "resume",
                "-C",
                "/tmp/project",
                "019abc",
            ],
        )
        self.assertEqual(runner.calls[0][1]["timeout"], 55.0)

    def test_nonzero_exit_raises_a_bounded_error(self):
        runner = FakeRunner([completed(stderr="not an agent", returncode=1)])

        with self.assertRaisesRegex(HerdrCLIError, "not an agent"):
            HerdrCLI(runner=runner).agent_focus("w1:p1")

    def test_invalid_json_is_rejected(self):
        runner = FakeRunner([completed("not-json")])

        with self.assertRaisesRegex(HerdrCLIError, "invalid JSON"):
            HerdrCLI(runner=runner).agent_list()

    def test_non_object_result_is_rejected(self):
        runner = FakeRunner([completed('{"result":[]}')])

        with self.assertRaisesRegex(HerdrCLIError, "non-object result"):
            HerdrCLI(runner=runner).agent_list()


if __name__ == "__main__":
    unittest.main()
