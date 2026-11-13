"""Softwarden MCP server: a thin client of the Softwarden REST API (/api/v1/) for MCP clients such as Claude Desktop.

It never touches the database: every answer comes from the API with the caller's API token, so the organization,
the role, read-only tokens and the edition's capabilities are enforced by Softwarden itself.

`client` and `tools` need only httpx2, so other code (the API's end-to-end test) can import them without the MCP SDK;
`server` wires them into the SDK."""

__version__ = "1.0.0"
