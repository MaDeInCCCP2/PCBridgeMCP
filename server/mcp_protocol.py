"""PCBridge transport constants shared by its server, clients and setup checks."""

# Keep the legacy handshake used by existing PCBridge clients and connection checks.
LEGACY_PROTOCOL_VERSION = "2025-03-26"
MCP_REQUEST_LIMIT = 32 * 1024 * 1024
