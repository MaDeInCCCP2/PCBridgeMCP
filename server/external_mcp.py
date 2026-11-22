"""Call explicitly granted tools from enabled, content-pinned portable plugins."""
import hashlib
import json
import logging
import os
import re
import sqlite3
from collections import OrderedDict
from contextlib import asynccontextmanager, closing
from math import ceil
from pathlib import Path
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import Client, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.types.version import MODERN_PROTOCOL_VERSIONS

from extension_packages import checked_path, fingerprint, plugin_path
from plugin_manager import component, read_json


class ExternalMCPError(ValueError):
    def __init__(self, code, message, **details):
        self.code = code
        self.details = details
        super().__init__(message)


class PluginBackoff:
    """Throttle repeated transport failures independently for each granted client."""
    def __init__(self, clock=monotonic):
        self.clock = clock
        self.failures = OrderedDict()
        self.lock = Lock()

    def check(self, key):
        with self.lock:
            _, available = self.failures.get(key, (0, 0))
            remaining = ceil(available - self.clock())
        if remaining > 0:
            raise ExternalMCPError("plugin_backoff", "The external MCP connection is temporarily cooling down.",
                                   retry_after_seconds=remaining)

    def failed(self, key):
        with self.lock:
            count, last = self.failures.pop(key, (0, 0))
            now = self.clock()
            count = min(5, count + 1) if now - last < 60 else 1
            available = now if count == 1 else now + 2 ** (count - 1)
            self.failures[key] = (count, available)
            if len(self.failures) > 256:
                self.failures.popitem(last=False)

    def succeeded(self, key):
        with self.lock:
            self.failures.pop(key, None)


_BACKOFF = PluginBackoff()


def sanitized(value, secrets):
    if isinstance(value, str):
        for credential in secrets:
            value = value.replace(credential, "[redacted]")
        return value
    if isinstance(value, list):
        return [sanitized(item, secrets) for item in value]
    if isinstance(value, dict):
        return {sanitized(key, secrets): sanitized(item, secrets) for key, item in value.items()}
    return value


def sensitive_fields(values):
    return {v for k, v in values.items()
            if v and re.search(r"token|secret|authorization|cookie|key|pass", k, re.IGNORECASE)}


def handoff_outcome(called):
    if called.is_error:
        return "tool_error"
    data = called.structured_content
    session_id = data.get("session_id") if isinstance(data, dict) and data.get("mode") == "win32_desktop" else None
    if isinstance(session_id, str) and re.fullmatch(r"[0-9a-f]{32}", session_id):
        return f"completed:{session_id}"
    return "unknown"


def handoff_receipt(operation_id, recorded, replayed=False):
    outcome, _, session_id = recorded.partition(":")
    result = {"ok": outcome == "completed" and bool(session_id), "operation_id": operation_id,
              "outcome": outcome, "already_dispatched": replayed}
    if result["ok"]:
        result["destination_session_id"] = session_id
    return result


class ExternalMCP:
    def __init__(self, registry, settings, backoff=_BACKOFF):
        self.backoff = backoff
        self.registry = registry
        self.settings = settings

    def prepare(self, plugin, server, principal):
        record = self.registry.get("plugin", plugin)
        grants = self.settings.get("plugin_mcp_grants", {})
        grant = grants.get(plugin, {}).get(server, {})
        permitted = grant.get("clients", {}).get(principal, [])
        if (not record["enabled"] or grant.get("sha256") != record["sha256"]
                or not isinstance(permitted, list) or not permitted or not all(isinstance(tool, str) for tool in permitted)):
            raise ExternalMCPError("plugin_permission_denied", "No local grant for this plugin, client and package version.")
        credentials = grant.get("credentials", {})
        if not isinstance(credentials, dict) or not all(
                isinstance(key, str) and isinstance(value, str) for key, value in credentials.items()):
            raise ExternalMCPError("plugin_grant_invalid", "Plugin credentials must map names to environment variables.")
        root = self.registry.package_root(record)
        if fingerprint(root) != record["sha256"]:
            raise ExternalMCPError("plugin_content_changed", "Local package content has changed since import.")
        settings = read_json(root, "mcp.json").get("mcpServers", {}).get(server)
        transport = component(root, server, settings)["transport"]
        if transport == "stdio" and not self.settings.get("enable_shell"):
            raise ExternalMCPError("plugin_shell_disabled", "The shell module must be enabled for stdio MCP.")
        if transport == "streamable-http" and not self.settings.get("enable_internet"):
            raise ExternalMCPError("plugin_network_disabled", "The internet module must be enabled for remote MCP.")
        if transport not in ("stdio", "streamable-http"):
            raise ExternalMCPError("plugin_transport_unsupported", "This MCP transport is unavailable.")
        return root, settings, set(permitted), credentials

    @asynccontextmanager
    async def connect(self, root, settings, credentials, plugin, server):
        # The owner names environment variables; grants and package metadata carry no secret values.
        injected = {name: os.environ.get(key) for name, key in credentials.items()}
        if any(not value for value in injected.values()):
            raise ExternalMCPError("plugin_credential_missing", "A configured MCP credential is unavailable.")
        secret_values = set(injected.values())
        if settings["type"] == "streamable-http":
            headers = {**settings.get("headers", {}), **injected}
            secret_values.update(sensitive_fields(headers))
            endpoint = urlsplit(settings["url"])
            if (secret_values and endpoint.scheme != "https"
                    and endpoint.hostname not in ("127.0.0.1", "::1", "localhost")):
                raise ExternalMCPError("plugin_tls_required", "Remote MCP credentials require HTTPS.")
            logging.getLogger("httpx2").setLevel(logging.WARNING)
            async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=20) as http:
                async with Client(streamable_http_client(settings["url"], http_client=http)) as client:
                    yield client, secret_values
            return

        # Hash the component keys because they are manifest strings, not paths.
        data_id = hashlib.sha256(json.dumps([plugin, server]).encode()).hexdigest()
        data = self.registry.root / "runtime" / data_id
        data.mkdir(parents=True, exist_ok=True)
        checked_path(data, self.registry.root.resolve())
        variables = {"PLUGIN_ROOT": str(root), "PLUGIN_DATA": str(data)}

        def expand(value):
            return re.sub(r"\$\{(PLUGIN_ROOT|PLUGIN_DATA)\}", lambda match: variables[match[1]], value)

        command = settings["command"]
        command = str(plugin_path(root, command, "command") or command)
        cwd = settings.get("cwd", "${PLUGIN_ROOT}")
        if cwd.startswith("${PLUGIN_DATA}"):
            cwd_path = Path(expand(cwd)).resolve()
            if not cwd_path.is_relative_to(data.resolve()) or not cwd_path.is_dir():
                raise ExternalMCPError("plugin_cwd_invalid", "Plugin data working directory is unavailable.")
        elif cwd.startswith("${PLUGIN_ROOT}"):
            cwd_path = Path(expand(cwd)).resolve()
            if not cwd_path.is_relative_to(root.resolve()) or not cwd_path.is_dir():
                raise ExternalMCPError("plugin_cwd_invalid", "Plugin working directory is unavailable.")
        else:
            cwd_path = plugin_path(root, cwd, "cwd")
        env = {**{key: expand(value) for key, value in settings.get("env", {}).items()}, **injected, **variables}
        secret_values.update(sensitive_fields(env))
        params = StdioServerParameters(command=command, cwd=cwd_path,
                                       args=[expand(arg) for arg in settings.get("args", [])], env=env)
        with open(os.devnull, "w", encoding="utf-8") as quiet:
            async with Client(stdio_client(params, errlog=quiet)) as client:
                yield client, secret_values

    def ledger(self, key, payload, outcome=None):
        with closing(sqlite3.connect(self.registry.root / "plugin-operations.sqlite3")) as database, database:
            database.execute("CREATE TABLE IF NOT EXISTS calls (id TEXT PRIMARY KEY, payload TEXT, outcome TEXT)")
            if outcome is not None:
                database.execute("UPDATE calls SET outcome = ? WHERE id = ?", (outcome, key))
                return None
            updated = database.execute("INSERT OR IGNORE INTO calls VALUES (?, ?, ?)", (key, payload, "unknown"))
            if updated.rowcount:
                return None
            previous, state = database.execute("SELECT payload, outcome FROM calls WHERE id = ?", (key,)).fetchone()
            if previous != payload:
                raise ExternalMCPError("plugin_operation_conflict", "This operation ID is already bound to another request.")
            return state

    @staticmethod
    def operation_key(plugin, server, principal, operation_id, handoff=None):
        if not isinstance(operation_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", operation_id):
            raise ValueError("Supply a unique operation ID of 16 to 64 characters.")
        fields = [plugin, server, principal, operation_id]
        if handoff is not None:
            fields.extend(["desktop_handoff", handoff])
        return hashlib.sha256(json.dumps(fields).encode()).hexdigest()

    def desktop_handoff_status(self, plugin, server, principal, peer, operation_id):
        _, settings, allowed, credentials = self.prepare(plugin, server, principal)
        self._require_peer_transport(settings, credentials)
        if "desktop_session_open" not in allowed:
            raise ExternalMCPError("plugin_permission_denied", "Tool is outside the local client grant.")
        key = self.operation_key(plugin, server, principal, operation_id, peer)
        ledger_path = self.registry.root / "plugin-operations.sqlite3"
        if ledger_path.is_file():
            with closing(sqlite3.connect(ledger_path.resolve().as_uri() + "?mode=ro", uri=True)) as database:
                if database.execute("SELECT name FROM sqlite_master WHERE name = 'calls'").fetchone():
                    found = database.execute("SELECT outcome FROM calls WHERE id = ?", (key,)).fetchone()
                    if found:
                        return handoff_receipt(operation_id, found[0], replayed=True)
        raise ExternalMCPError("desktop_handoff_missing", "No receipt for this operation on the selected peer.")

    @staticmethod
    def _require_peer_transport(settings, credentials):
        if settings["type"] != "streamable-http":
            raise ExternalMCPError("desktop_peer_transport", "A desktop peer needs a granted Streamable HTTP component.")
        if not any(name.lower() == "authorization" for name in credentials):
            raise ExternalMCPError("desktop_peer_credential", "A peer needs its own Authorization credential grant.")

    async def desktop_handoff(self, plugin, server, principal, peer, source, app, operation_id):
        return await self.run(plugin, server, principal, tool="desktop_session_open", arguments={"app": app},
                              operation_id=operation_id, _handoff=(peer, source))

    async def run(self, plugin, server, principal, *, tool=None, arguments=None, operation_id=None, _handoff=None):
        prepared = await anyio.to_thread.run_sync(self.prepare, plugin, server, principal)
        root, settings, allowed, credentials = prepared
        if _handoff is not None:
            self._require_peer_transport(settings, credentials)
        if tool is not None:
            if tool not in allowed:
                raise ExternalMCPError("plugin_permission_denied", "Tool is outside the local client grant.")
            key = self.operation_key(plugin, server, principal, operation_id,
                                     _handoff[0] if _handoff is not None else None)
            identity = [tool, arguments]
            if _handoff is not None:
                identity.append(_handoff)
            payload = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            prior = self.ledger(key, payload)
            if prior is not None:
                if _handoff is not None:
                    return handoff_receipt(operation_id, prior, replayed=True)
                return {"operation_id": operation_id, "outcome": prior, "already_dispatched": True,
                        "ok": prior == "completed"}
        dispatched = False
        result = None
        budget = (str(root), plugin, server, principal)
        try:
            self.backoff.check(budget)
            with anyio.fail_after(30):
                async with self.connect(root, settings, credentials, plugin, server) as (session, secrets):
                    if tool is None:
                        tools = await session.list_tools()
                        listed = [item.model_dump(mode="json", by_alias=True, exclude_none=True) for item in tools.tools
                                  if item.name in allowed]
                        result = {"tools": sanitized(listed, secrets)}
                    else:
                        # New peers may require routing headers derived from the tool schema.
                        # The low-level call sends one request without automatic follow-up rounds.
                        if session.protocol_version in MODERN_PROTOCOL_VERSIONS:
                            await session.list_tools()
                        dispatched = True
                        called = await session.session.call_tool(tool, arguments)
                        if _handoff is not None:
                            outcome = handoff_outcome(called)
                            self.ledger(key, payload, outcome)
                            result = handoff_receipt(operation_id, outcome)
                        else:
                            outcome = "tool_error" if called.is_error else "completed"
                            result = {"operation_id": operation_id, "outcome": outcome, "ok": not called.is_error,
                                      "result": sanitized(called.model_dump(mode="json", by_alias=True, exclude_none=True), secrets)}
                            self.ledger(key, payload, outcome)
        except Exception as error:
            if result is None:
                if not isinstance(error, ExternalMCPError):
                    self.backoff.failed(budget)
                if dispatched:
                    return {"ok": False, "error_code": "plugin_outcome_unknown", "operation_id": operation_id,
                            "outcome": "unknown"}
                if tool is not None:
                    self.ledger(key, payload, "failed_before_dispatch")
                if isinstance(error, ExternalMCPError):
                    raise
                raise ExternalMCPError("plugin_unavailable", "Could not complete the external MCP request.") from None
        self.backoff.succeeded(budget)
        return {**result, "source": "plugin", "trust": "untrusted_external"}
