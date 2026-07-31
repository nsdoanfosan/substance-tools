from __future__ import annotations

import json
import socketserver
import threading

import pytest

from substance_designer_mcp import bridge_client


class _Handler(socketserver.StreamRequestHandler):
    response = {"result": {"connected": True}}

    def handle(self):
        request = json.loads(self.rfile.readline().decode("utf-8"))
        response = {
            "id": request["id"],
            "ok": "error" not in self.response,
            **self.response,
        }
        self.wfile.write((json.dumps(response) + "\n").encode("utf-8"))


@pytest.fixture()
def fake_bridge(tmp_path, monkeypatch):
    server = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state_path = tmp_path / "bridge.json"
    state_path.write_text(
        json.dumps(
            {
                "host": "127.0.0.1",
                "port": server.server_address[1],
                "token": "test-token",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SUBSTANCE_DESIGNER_MCP_STATE", str(state_path))
    yield server
    server.shutdown()
    server.server_close()


def test_call_bridge_returns_result(fake_bridge):
    _Handler.response = {"result": {"connected": True}}
    assert bridge_client.call_bridge("status") == {"connected": True}


def test_call_bridge_raises_remote_error(fake_bridge):
    _Handler.response = {"error": {"message": "boom"}}
    with pytest.raises(bridge_client.DesignerBridgeError, match="boom"):
        bridge_client.call_bridge("status")
