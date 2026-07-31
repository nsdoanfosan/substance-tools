from __future__ import annotations

import json
import os
import socket
import uuid
from pathlib import Path
from typing import Any


DEFAULT_TIMEOUT = 30.0
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class DesignerBridgeError(RuntimeError):
    """Raised when the Designer-side bridge is unavailable or rejects a call."""


def state_file_path() -> Path:
    override = os.environ.get("SUBSTANCE_DESIGNER_MCP_STATE")
    if override:
        return Path(override).expanduser()
    local_app_data = os.environ.get("LOCALAPPDATA")
    if not local_app_data:
        raise DesignerBridgeError("LOCALAPPDATA is not set")
    return Path(local_app_data) / "SubstanceDesignerMCP" / "bridge.json"


def read_state() -> dict[str, Any]:
    path = state_file_path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DesignerBridgeError(
            "Substance Designer bridge is not running. Open Designer and load "
            "the substance_designer_mcp_bridge plugin."
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DesignerBridgeError(f"Could not read bridge state at {path}: {exc}") from exc

    for key in ("host", "port", "token"):
        if key not in state:
            raise DesignerBridgeError(f"Bridge state is missing {key!r}: {path}")
    return state


def call_bridge(
    method: str,
    params: dict[str, Any] | None = None,
    *,
    timeout: float = DEFAULT_TIMEOUT,
) -> Any:
    state = read_state()
    request_id = uuid.uuid4().hex
    request = {
        "id": request_id,
        "token": state["token"],
        "method": method,
        "params": params or {},
    }
    payload = (json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8")

    try:
        with socket.create_connection(
            (str(state["host"]), int(state["port"])), timeout=timeout
        ) as connection:
            connection.settimeout(timeout)
            connection.sendall(payload)
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise DesignerBridgeError("Designer response exceeded 16 MiB")
                if b"\n" in chunk:
                    break
    except (OSError, TimeoutError) as exc:
        raise DesignerBridgeError(
            f"Could not reach Substance Designer at {state['host']}:{state['port']}: {exc}"
        ) from exc

    raw = b"".join(chunks).split(b"\n", 1)[0]
    if not raw:
        raise DesignerBridgeError("Designer closed the connection without a response")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DesignerBridgeError(f"Designer returned invalid JSON: {exc}") from exc

    if response.get("id") != request_id:
        raise DesignerBridgeError("Designer response ID did not match the request")
    if not response.get("ok"):
        error = response.get("error") or {}
        message = error.get("message") or "Unknown Designer bridge error"
        details = error.get("traceback")
        if details:
            message = f"{message}\n{details}"
        raise DesignerBridgeError(message)
    return response.get("result")
