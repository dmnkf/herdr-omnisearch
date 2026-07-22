import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))

from herdr_omnisearch.herdr_socket import (  # noqa: E402
    HerdrAPIError,
    HerdrClient,
    resolve_socket_path,
)


class FakeSocket:
    def __init__(self, responder):
        self.responder = responder
        self.requests = []
        self.responses = b""
        self.connect_count = 0

    def connect(self, _path):
        self.connect_count += 1

    def settimeout(self, _timeout):
        pass

    def sendall(self, data):
        request = json.loads(data.decode().rstrip("\n"))
        self.requests.append(request)
        self.responses += b"".join(
            (json.dumps(message) + "\n").encode()
            for message in self.responder(request)
        )

    def recv(self, size):
        chunk, self.responses = self.responses[:size], self.responses[size:]
        return chunk

    def close(self):
        pass


def client_with(responder):
    fake = FakeSocket(responder)
    client = HerdrClient(
        "/tmp/herdr-test.sock",
        socket_factory=lambda _family, _kind: fake,
    )
    return client, fake


class HerdrClientTests(unittest.TestCase):
    def test_socket_path_resolution(self):
        self.assertEqual(
            resolve_socket_path(env={"HERDR_SESSION": "work"}, home="/home/test"),
            "/home/test/.config/herdr/sessions/work/herdr.sock",
        )
        self.assertEqual(
            resolve_socket_path(env={"HERDR_SOCKET_PATH": "/tmp/live.sock"}),
            "/tmp/live.sock",
        )

    def test_control_requests_use_fresh_connections(self):
        client, fake = client_with(
            lambda request: [{"id": request["id"], "result": {"type": "pong"}}]
        )
        client.request("ping")
        client.request("ping")
        self.assertEqual(fake.connect_count, 2)

    def test_snapshot_and_pane_read_use_protocol_methods(self):
        def responder(request):
            if request["method"] == "session.snapshot":
                result = {"type": "session_snapshot", "snapshot": {"panes": []}}
            else:
                result = {"type": "pane_read", "read": {"text": "hello"}}
            return [{"id": request["id"], "result": result}]

        client, fake = client_with(responder)
        self.assertEqual(client.snapshot(), {"panes": []})
        self.assertEqual(client.pane_read("w1:p2", 350), "hello")
        self.assertEqual(fake.requests[1]["method"], "pane.read")
        self.assertEqual(fake.requests[1]["params"]["source"], "recent_unwrapped")

    def test_subscription_keeps_connection_open(self):
        def responder(request):
            return [
                {"id": request["id"], "result": {"type": "subscription_started"}},
                {"event": "pane.scroll_changed", "data": {"pane_id": "w1:p2"}},
            ]

        client, fake = client_with(responder)
        client.subscribe([{"type": "pane.scroll_changed", "pane_id": "w1:p2"}])
        self.assertEqual(client.next_event(timeout=0.1)["event"], "pane.scroll_changed")
        self.assertEqual(fake.connect_count, 1)

    def test_overlay_plugin_pane_implicitly_targets_active_pane(self):
        client, fake = client_with(
            lambda request: [{"id": request["id"], "result": {"type": "plugin_pane_opened"}}]
        )
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}, clear=False):
            client.open_plugin_pane("live")
        params = fake.requests[0]["params"]
        self.assertEqual(params["plugin_id"], "herdr.omnisearch")
        self.assertEqual(params["entrypoint"], "live")
        self.assertNotIn("target_pane_id", params)

    def test_split_plugin_pane_uses_injected_target_pane(self):
        client, fake = client_with(
            lambda request: [{"id": request["id"], "result": {"type": "plugin_pane_opened"}}]
        )
        with patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}, clear=False):
            client.open_plugin_pane("live", placement="split")
        self.assertEqual(fake.requests[0]["params"]["target_pane_id"], "w1:p2")

    def test_api_errors_are_typed(self):
        client, _fake = client_with(
            lambda request: [{
                "id": request["id"],
                "error": {"code": "not_found", "message": "missing pane"},
            }]
        )
        with self.assertRaises(HerdrAPIError):
            client.focus_pane("missing")


if __name__ == "__main__":
    unittest.main()
