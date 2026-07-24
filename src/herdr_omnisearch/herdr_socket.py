"""Synchronous client for Herdr's newline-delimited JSON socket API."""

from __future__ import annotations

import json
import os
import socket
import time
from collections import deque
from pathlib import Path


class HerdrError(RuntimeError):
    """Base error for Herdr transport and API failures."""


class HerdrTransportError(HerdrError):
    pass


class HerdrTimeout(HerdrTransportError):
    pass


class HerdrAPIError(HerdrError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def resolve_socket_path(explicit=None, env=None, home=None):
    env = os.environ if env is None else env
    if explicit:
        return os.path.expanduser(explicit)
    if env.get("HERDR_SOCKET_PATH"):
        return os.path.expanduser(env["HERDR_SOCKET_PATH"])

    config_home = Path(home or Path.home()) / ".config" / "herdr"
    session = env.get("HERDR_SESSION")
    if session:
        return str(config_home / "sessions" / session / "herdr.sock")
    return str(config_home / "herdr.sock")


def socket_is_alive(socket_path: str, timeout: float = 0.5) -> bool:
    """Probe whether a Herdr session still accepts connections on this socket."""
    if not socket_path:
        return False
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.connect(socket_path)
        return True
    except OSError:
        return False
    finally:
        probe.close()


class HerdrClient:
    """Use fresh control connections and persistent subscription connections."""

    def __init__(self, socket_path=None, timeout=15.0, socket_factory=None):
        self.socket_path = resolve_socket_path(socket_path)
        self.timeout = timeout
        self.socket_factory = socket_factory or socket.socket
        self._socket = None
        self._buffer = b""
        self._counter = 0
        self._events = deque()
        self._responses = {}
        self._subscription = False

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()

    def connect(self):
        if self._socket is not None:
            return
        sock = None
        try:
            sock = self.socket_factory(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.socket_path)
        except OSError as exc:
            if sock is not None:
                sock.close()
            raise HerdrTransportError(
                f"cannot connect to Herdr socket {self.socket_path}: {exc}"
            ) from exc
        self._socket = sock

    def close(self):
        self._disconnect()
        self._events.clear()
        self._responses.clear()

    def _disconnect(self):
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None
                self._buffer = b""
                self._subscription = False

    def _deadline(self, timeout):
        return time.monotonic() + (self.timeout if timeout is None else timeout)

    def _read_message(self, deadline):
        self.connect()
        while b"\n" not in self._buffer:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HerdrTimeout("timed out waiting for Herdr")
            try:
                self._socket.settimeout(remaining)
                chunk = self._socket.recv(65536)
            except socket.timeout as exc:
                raise HerdrTimeout("timed out waiting for Herdr") from exc
            except OSError as exc:
                self._disconnect()
                raise HerdrTransportError(f"Herdr socket read failed: {exc}") from exc
            if not chunk:
                self._disconnect()
                raise HerdrTransportError("Herdr closed the socket")
            self._buffer += chunk

        line, self._buffer = self._buffer.split(b"\n", 1)
        try:
            return json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HerdrTransportError(f"invalid JSON from Herdr: {line[:200]!r}") from exc

    def request(self, method, params=None, timeout=None, keep_open=False):
        if self._subscription and not keep_open:
            raise HerdrTransportError(
                "subscription connections only receive events; use a separate client"
            )
        self.connect()
        self._counter += 1
        request_id = f"omnisearch:{os.getpid()}:{self._counter}"
        payload = {"id": request_id, "method": method, "params": params or {}}
        encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
        try:
            self._socket.sendall(encoded)
        except OSError as exc:
            self._disconnect()
            raise HerdrTransportError(f"Herdr socket write failed: {exc}") from exc

        deadline = self._deadline(timeout)
        try:
            if request_id in self._responses:
                message = self._responses.pop(request_id)
            else:
                while True:
                    message = self._read_message(deadline)
                    message_id = message.get("id")
                    if message_id == request_id:
                        break
                    if message_id:
                        self._responses[message_id] = message
                    else:
                        self._events.append(message)
        except HerdrError:
            self._disconnect()
            raise

        if not keep_open:
            self._disconnect()
        if "error" in message:
            if keep_open:
                self._disconnect()
            error = message.get("error") or {}
            raise HerdrAPIError(error.get("code", "api_error"), error.get("message", "unknown error"))
        if "result" not in message:
            if keep_open:
                self._disconnect()
            raise HerdrTransportError(f"Herdr response has no result: {message!r}")
        if keep_open:
            self._subscription = True
        return message["result"]

    def snapshot(self):
        return self.request("session.snapshot")["snapshot"]

    def pane_read(self, pane_id, lines):
        result = self.request(
            "pane.read",
            {
                "pane_id": pane_id,
                "source": "recent_unwrapped",
                "lines": lines,
                "format": "text",
                "strip_ansi": True,
            },
        )
        return result["read"]["text"]

    def focus_workspace(self, workspace_id):
        return self.request("workspace.focus", {"workspace_id": workspace_id})

    def focus_tab(self, tab_id):
        return self.request("tab.focus", {"tab_id": tab_id})

    def focus_pane(self, pane_id):
        return self.request("pane.focus", {"pane_id": pane_id})

    def rename_workspace(self, workspace_id, label):
        return self.request("workspace.rename", {"workspace_id": workspace_id, "label": label})

    def rename_pane(self, pane_id, label):
        return self.request("pane.rename", {"pane_id": pane_id, "label": label})

    def create_workspace(self, cwd, label, focus):
        return self.request(
            "workspace.create",
            {"cwd": cwd, "label": label, "focus": focus, "env": {}},
        )

    def send_input(self, pane_id, text):
        return self.request(
            "pane.send_input",
            {"pane_id": pane_id, "text": text, "keys": ["enter"]},
        )

    def open_plugin_pane(self, entrypoint, placement="overlay", focus=True):
        params = {
            "plugin_id": "herdr.omnisearch",
            "entrypoint": entrypoint,
            "placement": placement,
            "focus": focus,
            "env": {},
        }
        pane_id = os.environ.get("HERDR_PANE_ID") or os.environ.get("HERDR_ACTIVE_PANE_ID")
        workspace_id = os.environ.get("HERDR_WORKSPACE_ID") or os.environ.get("HERDR_ACTIVE_WORKSPACE_ID")
        if pane_id and placement in {"split", "zoomed"}:
            params["target_pane_id"] = pane_id
        if workspace_id and placement == "tab":
            params["workspace_id"] = workspace_id
        return self.request("plugin.pane.open", params)

    def subscribe(self, subscriptions):
        return self.request(
            "events.subscribe",
            {"subscriptions": subscriptions},
            keep_open=True,
        )

    def next_event(self, timeout=None):
        if self._events:
            return self._events.popleft()
        deadline = self._deadline(timeout)
        while True:
            message = self._read_message(deadline)
            if not message.get("id"):
                return message
            self._responses[message["id"]] = message
