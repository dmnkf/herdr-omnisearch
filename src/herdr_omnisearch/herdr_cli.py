"""Adapter for Herdr's stable agent automation CLI."""

from __future__ import annotations

import json
import os
import subprocess


class HerdrCLIError(RuntimeError):
    """Raised when a Herdr CLI command fails or returns an invalid response."""


class HerdrCLI:
    def __init__(self, binary="herdr", runner=None, env=None):
        self.binary = binary
        self.runner = runner or subprocess.run
        # Extra environment (e.g. HERDR_SOCKET_PATH) to target another session.
        self.env = dict(env) if env else None

    def _run(self, args, *, timeout=30, json_output=False):
        command = [self.binary, *args]
        run_env = {**os.environ, **self.env} if self.env else None
        try:
            result = self.runner(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
                env=run_env,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise HerdrCLIError(f"could not run {self.binary}: {exc}") from exc

        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "unknown error").strip()
            raise HerdrCLIError(f"{' '.join(args[:3])} failed: {detail[:500]}")

        if not json_output:
            return result.stdout
        try:
            payload = json.loads(result.stdout)
            parsed = payload["result"]
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise HerdrCLIError(
                f"{' '.join(args[:3])} returned invalid JSON: {result.stdout[:500]!r}"
            ) from exc
        if not isinstance(parsed, dict):
            raise HerdrCLIError(f"{' '.join(args[:3])} returned a non-object result")
        return parsed

    def agent_list(self):
        result = self._run(["agent", "list"], json_output=True)
        agents = result.get("agents")
        if not isinstance(agents, list):
            raise HerdrCLIError("agent list response has no agents array")
        return agents

    def agent_read(self, target, lines):
        return self._run(
            [
                "agent",
                "read",
                target,
                "--source",
                "recent-unwrapped",
                "--lines",
                str(lines),
                "--format",
                "text",
            ]
        )

    def agent_focus(self, target):
        return self._run(["agent", "focus", target], json_output=True)

    def agent_start(self, name, kind, pane_id, agent_args=None, timeout_ms=60000):
        args = [
            "agent",
            "start",
            name,
            "--kind",
            kind,
            "--pane",
            pane_id,
            "--timeout",
            str(timeout_ms),
        ]
        if agent_args:
            args.extend(["--", *agent_args])
        return self._run(args, timeout=(timeout_ms / 1000) + 10, json_output=True)
