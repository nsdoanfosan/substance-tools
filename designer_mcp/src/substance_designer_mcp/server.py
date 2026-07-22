from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .bridge_client import call_bridge, read_state


INSTRUCTIONS = """Use this server only while Adobe Substance 3D Designer is open and the bridge plugin is loaded. Inspect active graph and node/property identifiers before mutating. Prefer dedicated tools for ordinary edits; use execute_python only for Designer Python API operations not covered by a dedicated tool. Mutations affect the live Designer document. Saving is explicit and never happens automatically."""

mcp = FastMCP("Substance 3D Designer", instructions=INSTRUCTIONS, json_response=True)


@mcp.tool()
def designer_status() -> dict[str, Any]:
    """Check the live Designer bridge and return application/session information."""
    state = read_state()
    result = call_bridge("status")
    public_state = {key: value for key, value in state.items() if key != "token"}
    return {"bridge": public_state, "designer": result}


@mcp.tool()
def list_packages() -> list[dict[str, Any]]:
    """List loaded user packages and their graph resources."""
    return call_bridge("list_packages")


@mcp.tool()
def get_active_graph(include_properties: bool = False) -> dict[str, Any]:
    """Inspect the current graph, its selected nodes, and all graph nodes."""
    return call_bridge(
        "get_active_graph", {"include_properties": include_properties}
    )


@mcp.tool()
def list_node_definitions(search: str = "", limit: int = 100) -> list[dict[str, str]]:
    """List node definitions available in the active graph, optionally filtered."""
    return call_bridge("list_node_definitions", {"search": search, "limit": limit})


@mcp.tool()
def create_node(definition_id: str, x: float = 0, y: float = 0) -> dict[str, Any]:
    """Create a node in the active graph and return its identifier and properties."""
    return call_bridge(
        "create_node", {"definition_id": definition_id, "x": x, "y": y}
    )


@mcp.tool()
def move_node(node_id: str, x: float, y: float) -> dict[str, Any]:
    """Move a node in the active graph. Coordinates use Designer graph units."""
    return call_bridge("move_node", {"node_id": node_id, "x": x, "y": y})


@mcp.tool()
def connect_nodes(
    source_node_id: str,
    source_property_id: str,
    target_node_id: str,
    target_property_id: str,
) -> dict[str, Any]:
    """Connect one output property to one input property in the active graph."""
    return call_bridge(
        "connect_nodes",
        {
            "source_node_id": source_node_id,
            "source_property_id": source_property_id,
            "target_node_id": target_node_id,
            "target_property_id": target_property_id,
        },
    )


@mcp.tool()
def set_input_value(
    node_id: str,
    property_id: str,
    value: Any,
    value_type: str = "auto",
    enum_type_id: str = "",
) -> dict[str, Any]:
    """Set an input value. Supports bool/int/float/string, numeric vectors, colors, and enums."""
    return call_bridge(
        "set_input_value",
        {
            "node_id": node_id,
            "property_id": property_id,
            "value": value,
            "value_type": value_type,
            "enum_type_id": enum_type_id,
        },
    )


@mcp.tool()
def save_package(file_path: str = "") -> dict[str, Any]:
    """Save the active graph's package, or Save As when file_path is supplied."""
    return call_bridge("save_package", {"file_path": file_path})


@mcp.tool()
def execute_python(code: str) -> dict[str, Any]:
    """Execute Python on Designer's main thread in a persistent namespace.

    The namespace includes sd, app, ui_mgr, package_mgr, and context. The value
    of the final expression is returned when possible, along with stdout/stderr.
    """
    return call_bridge("execute_python", {"code": code}, timeout=60.0)


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
