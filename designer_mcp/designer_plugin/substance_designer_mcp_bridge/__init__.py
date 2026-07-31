from __future__ import annotations

import ast
import contextlib
import io
import json
import logging
import os
import secrets
import socketserver
import tempfile
import threading
import traceback
from pathlib import Path

import sd
from PySide6 import QtCore
from sd.api.sdhistoryutils import SDHistoryUtils
from sd.api.sdproperty import SDPropertyCategory
from sd.api.sdbasetypes import (
    ColorRGB,
    ColorRGBA,
    bool2,
    bool3,
    bool4,
    double2,
    double3,
    double4,
    float2,
    float3,
    float4,
    int2,
    int3,
    int4,
)
from sd.api.sdvaluebool import SDValueBool
from sd.api.sdvaluebool2 import SDValueBool2
from sd.api.sdvaluebool3 import SDValueBool3
from sd.api.sdvaluebool4 import SDValueBool4
from sd.api.sdvaluecolorrgb import SDValueColorRGB
from sd.api.sdvaluecolorrgba import SDValueColorRGBA
from sd.api.sdvaluedouble import SDValueDouble
from sd.api.sdvaluedouble2 import SDValueDouble2
from sd.api.sdvaluedouble3 import SDValueDouble3
from sd.api.sdvaluedouble4 import SDValueDouble4
from sd.api.sdvalueenum import SDValueEnum
from sd.api.sdvaluefloat import SDValueFloat
from sd.api.sdvaluefloat2 import SDValueFloat2
from sd.api.sdvaluefloat3 import SDValueFloat3
from sd.api.sdvaluefloat4 import SDValueFloat4
from sd.api.sdvalueint import SDValueInt
from sd.api.sdvalueint2 import SDValueInt2
from sd.api.sdvalueint3 import SDValueInt3
from sd.api.sdvalueint4 import SDValueInt4
from sd.api.sdvaluestring import SDValueString


LOGGER = logging.getLogger("SubstanceDesignerMCP")
HOST = "127.0.0.1"
PORTS = range(18888, 18899)
REQUEST_TIMEOUT_SECONDS = 60.0
MAX_REQUEST_BYTES = 4 * 1024 * 1024

_dispatcher = None
_server = None
_server_thread = None
_token = None
_state_path = None
_python_namespace = None


def _safe_call(obj, method, default=None):
    try:
        return getattr(obj, method)()
    except Exception:
        return default


def _jsonify(value, depth=0):
    if depth > 8:
        return repr(value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonify(item, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonify(item, depth + 1) for item in value]
    fields = getattr(value, "_fields_", None)
    if fields:
        return {name: _jsonify(getattr(value, name), depth + 1) for name, _ in fields}
    if value.__class__.__name__ == "SDValueEnum":
        return {"value": value.get(), "value_id": value.getValueId()}
    getter = getattr(value, "get", None)
    if callable(getter):
        try:
            return _jsonify(getter(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "__iter__") and not isinstance(value, (bytes, bytearray)):
        try:
            return [_jsonify(item, depth + 1) for item in value]
        except Exception:
            pass
    summary = {"class": value.__class__.__name__}
    for method, key in (
        ("getIdentifier", "identifier"),
        ("getId", "id"),
        ("getLabel", "label"),
        ("getUrl", "url"),
        ("getFilePath", "file_path"),
    ):
        result = _safe_call(value, method)
        if result not in (None, ""):
            summary[key] = _jsonify(result, depth + 1)
    if len(summary) > 1:
        return summary
    return repr(value)


def _property_summary(node, prop, category):
    prop_type = _safe_call(prop, "getType")
    value = None
    try:
        value = node.getPropertyValue(prop)
    except Exception:
        pass
    return {
        "id": _safe_call(prop, "getId", ""),
        "label": _safe_call(prop, "getLabel", ""),
        "category": category.name.lower(),
        "type": _safe_call(prop_type, "getId", ""),
        "value": _jsonify(value),
    }


def _node_summary(node, include_properties=False):
    definition = _safe_call(node, "getDefinition")
    position = _safe_call(node, "getPosition")
    result = {
        "id": _safe_call(node, "getIdentifier", ""),
        "definition_id": _safe_call(definition, "getId", ""),
        "label": _safe_call(definition, "getLabel", ""),
        "position": _jsonify(position),
    }
    if include_properties:
        properties = []
        for category in (
            SDPropertyCategory.Input,
            SDPropertyCategory.Output,
            SDPropertyCategory.Annotation,
        ):
            try:
                properties.extend(
                    _property_summary(node, prop, category)
                    for prop in node.getProperties(category)
                )
            except Exception:
                continue
        result["properties"] = properties
    return result


def _active_graph():
    app = sd.getContext().getSDApplication()
    ui_mgr = app.getUIMgr()
    graph = ui_mgr.getCurrentGraph() if ui_mgr else None
    if graph is None:
        raise RuntimeError("No active graph is open in Substance Designer")
    return graph


def _active_node(node_id):
    graph = _active_graph()
    node = graph.getNodeFromId(node_id)
    if node is None:
        raise ValueError(f"Node not found in active graph: {node_id}")
    return graph, node


def _package_summary(package):
    graphs = []
    for resource in package.getChildrenResources(True):
        if hasattr(resource, "getNodes"):
            graphs.append(
                {
                    "identifier": _safe_call(resource, "getIdentifier", ""),
                    "url": _safe_call(resource, "getUrl", ""),
                    "type": resource.__class__.__name__,
                }
            )
    return {
        "uid": _safe_call(package, "getUID", ""),
        "file_path": _safe_call(package, "getFilePath", ""),
        "modified": bool(_safe_call(package, "isModified", False)),
        "graphs": graphs,
    }


def _status(_params):
    app = sd.getContext().getSDApplication()
    graph = None
    try:
        graph = _active_graph()
    except RuntimeError:
        pass
    return {
        "connected": True,
        "version": app.getVersion(),
        "process_id": os.getpid(),
        "active_graph": _safe_call(graph, "getUrl") if graph else None,
    }


def _list_packages(_params):
    manager = sd.getContext().getSDApplication().getPackageMgr()
    return [_package_summary(package) for package in manager.getUserPackages()]


def _get_active_graph(params):
    graph = _active_graph()
    package = graph.getPackage()
    app = sd.getContext().getSDApplication()
    ui_mgr = app.getUIMgr()
    selected = ui_mgr.getCurrentGraphSelectedNodes() if ui_mgr else []
    include_properties = bool(params.get("include_properties", False))
    return {
        "identifier": _safe_call(graph, "getIdentifier", ""),
        "url": _safe_call(graph, "getUrl", ""),
        "type": graph.__class__.__name__,
        "package": _package_summary(package),
        "selected_node_ids": [
            _safe_call(node, "getIdentifier", "") for node in (selected or [])
        ],
        "nodes": [
            _node_summary(node, include_properties) for node in graph.getNodes()
        ],
    }


def _list_node_definitions(params):
    graph = _active_graph()
    search = str(params.get("search", "")).casefold()
    limit = max(1, min(int(params.get("limit", 100)), 1000))
    items = []
    for definition in graph.getNodeDefinitions():
        item = {
            "id": _safe_call(definition, "getId", ""),
            "label": _safe_call(definition, "getLabel", ""),
            "description": _safe_call(definition, "getDescription", ""),
        }
        haystack = " ".join(str(value) for value in item.values()).casefold()
        if search and search not in haystack:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def _create_node(params):
    graph = _active_graph()
    definition_id = str(params["definition_id"])
    with SDHistoryUtils.UndoGroup("MCP: Create Node"):
        node = graph.newNode(definition_id)
        node.setPosition(float2(float(params.get("x", 0)), float(params.get("y", 0))))
    return _node_summary(node, True)


def _move_node(params):
    _graph, node = _active_node(str(params["node_id"]))
    with SDHistoryUtils.UndoGroup("MCP: Move Node"):
        node.setPosition(float2(float(params["x"]), float(params["y"])))
    return _node_summary(node, False)


def _connect_nodes(params):
    graph, source = _active_node(str(params["source_node_id"]))
    target = graph.getNodeFromId(str(params["target_node_id"]))
    if target is None:
        raise ValueError(f"Target node not found: {params['target_node_id']}")
    with SDHistoryUtils.UndoGroup("MCP: Connect Nodes"):
        source.newPropertyConnectionFromId(
            str(params["source_property_id"]),
            target,
            str(params["target_property_id"]),
        )
    return {
        "source": _node_summary(source, False),
        "target": _node_summary(target, False),
        "source_property_id": str(params["source_property_id"]),
        "target_property_id": str(params["target_property_id"]),
    }


def _sequence(value, length, cast=float):
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise ValueError(f"Expected an array with {length} values")
    return [cast(item) for item in value]


def _make_sd_value(value, value_type, enum_type_id=""):
    factories = {
        "bool": lambda: SDValueBool.sNew(bool(value)),
        "int": lambda: SDValueInt.sNew(int(value)),
        "float": lambda: SDValueFloat.sNew(float(value)),
        "double": lambda: SDValueDouble.sNew(float(value)),
        "string": lambda: SDValueString.sNew(str(value)),
        "bool2": lambda: SDValueBool2.sNew(bool2(*_sequence(value, 2, bool))),
        "bool3": lambda: SDValueBool3.sNew(bool3(*_sequence(value, 3, bool))),
        "bool4": lambda: SDValueBool4.sNew(bool4(*_sequence(value, 4, bool))),
        "int2": lambda: SDValueInt2.sNew(int2(*_sequence(value, 2, int))),
        "int3": lambda: SDValueInt3.sNew(int3(*_sequence(value, 3, int))),
        "int4": lambda: SDValueInt4.sNew(int4(*_sequence(value, 4, int))),
        "float2": lambda: SDValueFloat2.sNew(float2(*_sequence(value, 2))),
        "float3": lambda: SDValueFloat3.sNew(float3(*_sequence(value, 3))),
        "float4": lambda: SDValueFloat4.sNew(float4(*_sequence(value, 4))),
        "double2": lambda: SDValueDouble2.sNew(double2(*_sequence(value, 2))),
        "double3": lambda: SDValueDouble3.sNew(double3(*_sequence(value, 3))),
        "double4": lambda: SDValueDouble4.sNew(double4(*_sequence(value, 4))),
        "colorrgb": lambda: SDValueColorRGB.sNew(ColorRGB(*_sequence(value, 3))),
        "colorrgba": lambda: SDValueColorRGBA.sNew(ColorRGBA(*_sequence(value, 4))),
    }
    normalized = value_type.casefold().replace(" ", "")
    if normalized == "enum":
        if not enum_type_id:
            raise ValueError("enum_type_id is required for enum values")
        if isinstance(value, str):
            return SDValueEnum.sFromValueId(enum_type_id, value)
        return SDValueEnum.sFromValue(enum_type_id, int(value))
    factory = factories.get(normalized)
    if factory is None:
        raise ValueError(f"Unsupported value type: {value_type}")
    return factory()


def _set_input_value(params):
    _graph, node = _active_node(str(params["node_id"]))
    property_id = str(params["property_id"])
    prop = node.getPropertyFromId(property_id, SDPropertyCategory.Input)
    if prop is None:
        raise ValueError(f"Input property not found: {property_id}")
    value_type = str(params.get("value_type", "auto"))
    if value_type == "auto":
        prop_type = prop.getType()
        current_value = node.getPropertyValue(prop)
        if current_value is not None and current_value.__class__.__name__ == "SDValueEnum":
            value_type = "enum"
            params["enum_type_id"] = prop_type.getId()
        else:
            value_type = prop_type.getId()
    sd_value = _make_sd_value(
        params.get("value"), value_type, str(params.get("enum_type_id", ""))
    )
    with SDHistoryUtils.UndoGroup("MCP: Set Input Value"):
        node.setInputPropertyValueFromId(property_id, sd_value)
    return {
        "node": _node_summary(node, False),
        "property": _property_summary(node, prop, SDPropertyCategory.Input),
    }


def _save_package(params):
    graph = _active_graph()
    package = graph.getPackage()
    manager = sd.getContext().getSDApplication().getPackageMgr()
    file_path = str(params.get("file_path", "")).strip()
    if file_path:
        path = str(Path(file_path).expanduser().resolve())
        manager.savePackageAs(package, path)
    else:
        path = package.getFilePath()
        if not path:
            raise ValueError("Active package has no file path; provide file_path for Save As")
        manager.savePackage(package)
    return {"saved": True, "file_path": path, "modified": package.isModified()}


def _execute_python(params):
    global _python_namespace
    code = str(params.get("code", ""))
    if not code.strip():
        raise ValueError("code must not be empty")
    app = sd.getContext().getSDApplication()
    if _python_namespace is None:
        _python_namespace = {
            "__name__": "substance_designer_mcp_console",
            "sd": sd,
        }
    _python_namespace.update(
        {
            "context": sd.getContext(),
            "app": app,
            "ui_mgr": app.getUIMgr(),
            "package_mgr": app.getPackageMgr(),
        }
    )
    stdout = io.StringIO()
    stderr = io.StringIO()
    tree = ast.parse(code, mode="exec")
    result = None
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            prefix = ast.Module(body=tree.body[:-1], type_ignores=[])
            if prefix.body:
                exec(compile(prefix, "<designer-mcp>", "exec"), _python_namespace)
            expression = ast.Expression(tree.body[-1].value)
            result = eval(compile(expression, "<designer-mcp>", "eval"), _python_namespace)
        else:
            exec(compile(tree, "<designer-mcp>", "exec"), _python_namespace)
    return {
        "result": _jsonify(result),
        "stdout": stdout.getvalue(),
        "stderr": stderr.getvalue(),
    }


_METHODS = {
    "status": _status,
    "list_packages": _list_packages,
    "get_active_graph": _get_active_graph,
    "list_node_definitions": _list_node_definitions,
    "create_node": _create_node,
    "move_node": _move_node,
    "connect_nodes": _connect_nodes,
    "set_input_value": _set_input_value,
    "save_package": _save_package,
    "execute_python": _execute_python,
}


class _PendingCall:
    def __init__(self, method, params):
        self.method = method
        self.params = params
        self.event = threading.Event()
        self.result = None
        self.error = None


class _MainThreadDispatcher(QtCore.QObject):
    requested = QtCore.Signal(object)

    def __init__(self):
        super().__init__()
        self.requested.connect(self._run, QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.Slot(object)
    def _run(self, pending):
        try:
            handler = _METHODS.get(pending.method)
            if handler is None:
                raise ValueError(f"Unknown bridge method: {pending.method}")
            pending.result = handler(pending.params)
        except Exception as exc:
            pending.error = {
                "type": exc.__class__.__name__,
                "message": str(exc),
                "traceback": traceback.format_exc(),
            }
        finally:
            pending.event.set()

    def dispatch(self, method, params):
        pending = _PendingCall(method, params)
        if QtCore.QThread.currentThread() == self.thread():
            self._run(pending)
        else:
            self.requested.emit(pending)
            if not pending.event.wait(REQUEST_TIMEOUT_SECONDS):
                raise TimeoutError(f"Designer main thread timed out running {method}")
        if pending.error:
            error = RuntimeError(pending.error["message"])
            error.bridge_error = pending.error
            raise error
        return pending.result


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self):
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        request_id = None
        try:
            if len(raw) > MAX_REQUEST_BYTES:
                raise ValueError("Request exceeded 4 MiB")
            request = json.loads(raw.decode("utf-8"))
            request_id = request.get("id")
            if not secrets.compare_digest(str(request.get("token", "")), _token or ""):
                raise PermissionError("Invalid bridge token")
            result = _dispatcher.dispatch(
                str(request.get("method", "")), request.get("params") or {}
            )
            response = {"id": request_id, "ok": True, "result": result}
        except Exception as exc:
            details = getattr(exc, "bridge_error", None)
            response = {
                "id": request_id,
                "ok": False,
                "error": details
                or {
                    "type": exc.__class__.__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                },
            }
        self.wfile.write((json.dumps(response, ensure_ascii=False) + "\n").encode("utf-8"))


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def _bridge_state_path():
    root = Path(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()))
    return root / "SubstanceDesignerMCP" / "bridge.json"


def _write_state(port):
    global _state_path
    _state_path = _bridge_state_path()
    _state_path.parent.mkdir(parents=True, exist_ok=True)
    state = {
        "host": HOST,
        "port": port,
        "token": _token,
        "process_id": os.getpid(),
        "designer_version": sd.getContext().getSDApplication().getVersion(),
        "plugin_version": "0.1.0",
    }
    temporary = _state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    os.replace(temporary, _state_path)


def initializeSDPlugin():
    global _dispatcher, _server, _server_thread, _token
    if _server is not None:
        return
    _dispatcher = _MainThreadDispatcher()
    _token = secrets.token_urlsafe(32)
    last_error = None
    for port in PORTS:
        try:
            _server = _ThreadingServer((HOST, port), _RequestHandler)
            break
        except OSError as exc:
            last_error = exc
    if _server is None:
        raise RuntimeError(f"No free Designer MCP bridge port: {last_error}")
    _server_thread = threading.Thread(
        target=_server.serve_forever,
        name="SubstanceDesignerMCPBridge",
        daemon=True,
    )
    _server_thread.start()
    _write_state(_server.server_address[1])
    LOGGER.info("Substance Designer MCP bridge listening on %s:%s", *_server.server_address)


def uninitializeSDPlugin():
    global _dispatcher, _server, _server_thread, _token, _state_path
    if _server is not None:
        _server.shutdown()
        _server.server_close()
    if _server_thread is not None:
        _server_thread.join(timeout=2.0)
    if _state_path is not None and _state_path.exists():
        try:
            state = json.loads(_state_path.read_text(encoding="utf-8"))
            if state.get("token") == _token:
                _state_path.unlink()
        except Exception:
            LOGGER.exception("Could not remove Designer MCP state file")
    _dispatcher = None
    _server = None
    _server_thread = None
    _token = None
    _state_path = None
    LOGGER.info("Substance Designer MCP bridge stopped")
