# Substance 3D Designer MCP

Local MCP control for Adobe Substance 3D Designer. It consists of:

- a Designer Python plugin that exposes the live `sd` API on loopback only;
- a stdio MCP server used by Codex and other MCP clients.

The bridge marshals every Designer API operation onto Qt's main thread. A
per-session token is written to
`%LOCALAPPDATA%\SubstanceDesignerMCP\bridge.json`; the MCP server reads that
file automatically.

## Requirements

- Adobe Substance 3D Designer 14.0 or newer (tested with 16.0.4)
- Python 3.10 or newer for the external MCP server
- `uv` for installation and launch

## Install

1. Make the plugin folder available under Designer's user plugin directory:

   `Documents\Adobe\Adobe Substance 3D Designer\python\sduserplugins\substance_designer_mcp_bridge`

   A directory junction to
   `designer_plugin\substance_designer_mcp_bridge` is recommended so source
   edits remain Git-tracked.

2. In Designer, use `Tools > Plugin Manager...`, open the plugin's
   `__init__.py`, and keep it loaded. The plugin is discovered automatically on
   future Designer starts when it is in the user plugin directory.

3. Create the MCP environment:

   ```powershell
   uv sync
   ```

4. Add this stdio server to the MCP client:

   ```toml
   [mcp_servers.substance_designer]
   command = "C:\\path\\to\\designer_mcp\\.venv\\Scripts\\python.exe"
   args = ["-m", "substance_designer_mcp.server"]
   cwd = "C:\\path\\to\\designer_mcp"
   startup_timeout_sec = 20
   tool_timeout_sec = 60
   enabled = true
   required = false
   ```

Restart the MCP client after changing its configuration.

## Tools

- `designer_status`
- `list_packages`
- `get_active_graph`
- `list_node_definitions`
- `create_node`
- `move_node`
- `connect_nodes`
- `set_input_value`
- `save_package`
- `execute_python`

Edits affect the live graph. The server never saves automatically.
