PCBridge 1.7.0 - Local Streamable HTTP with a token in the URL

Use when an MCP client only provides a URL and cannot send the Authorization header.

1. Start the normal PCBridge instance via START.bat.
2. Start TOKEN_HTTP.bat in this copy (keep it running).
3. Open TOKEN.txt locally (do not send the token to anyone).
4. Configure the MCP client:
   Type: StreamableHTTP
   URL: http://127.0.0.1:8770/<TOKEN.txt value>/mcp

The URL contains a SECRET. Do not paste it into chats, screenshots, logs, or
public bug reports. This convenience method is less secure than an HTTP
Authorization header because clients may log URLs. Use it only with a local
desktop client on the same PC.

The adapter listens only on 127.0.0.1:8770. Never configure ngrok to expose
port 8770. The upstream endpoint on port 8765 stays unchanged and requires
Authorization: Bearer <token>. Token is checked on every request.
No token contents are printed by the adapter or launcher.

Health check (no token): http://127.0.0.1:8770/health
To stop: close the TOKEN_HTTP.bat window or press Ctrl+C.

Version remains 1.7.0.
