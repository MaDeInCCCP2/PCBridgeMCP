"""Inspect portable Agent Plugins 1.0 components without loading their runtimes."""
import json
import re
from urllib.parse import urlsplit

from extension_packages import plugin_path, read_member
from skills_manager import parse as parse_skill


SCHEMAS = "https://agent-plugins.org/schemas/1.0.0/"
NAME = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?")
TEXT_FIELDS = {"version", "description", "license", "homepage", "repository"}


def read_json(root, filename):
    def reject_duplicates(pairs):
        result = dict(pairs)
        if len(result) != len(pairs):
            raise ValueError("Duplicate JSON field")
        return result

    return json.loads(read_member(root / filename, root, 256 * 1024),
                      object_pairs_hook=reject_duplicates)


def valid_string_map(obj):
    return isinstance(obj, dict) and all(isinstance(value, str) for value in obj.values())


def component(root, name, config):
    if not isinstance(config, dict):
        raise ValueError
    transport = config.get("type")
    if transport == "stdio":
        if set(config) - {"type", "command", "args", "env", "cwd"}:
            raise ValueError
        command, cwd = config.get("command"), config.get("cwd")
        args, env = config.get("args", []), config.get("env", {})
        if (not isinstance(command, str) or not command or not isinstance(args, list)
                or not all(isinstance(arg, str) for arg in args) or not valid_string_map(env)
                or {"PLUGIN_ROOT", "PLUGIN_DATA"} & env.keys()):
            raise ValueError
        plugin_path(root, command, "command")
        if cwd is not None:
            if not isinstance(cwd, str):
                raise ValueError
            plugin_path(root, cwd, "cwd")
    elif transport in ("streamable-http", "sse"):
        if (set(config) - {"type", "url", "headers"} or not isinstance(config.get("url"), str)
                or not valid_string_map(config.get("headers", {}))):
            raise ValueError
        url = urlsplit(config["url"])
        if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.fragment:
            raise ValueError
    else:
        raise ValueError
    return {"name": name, "transport": transport, "state": "discovered"}


def inspect(root):
    manifest = read_json(root, "plugin.json")
    if not isinstance(manifest, dict) or manifest.get("$schema") != SCHEMAS + "plugin.schema.json":
        raise ValueError("Use a portable Agent Plugins 1.0 manifest at plugin.json")
    name = manifest.get("name")
    if not isinstance(name, str) or not NAME.fullmatch(name) or "--" in name or ".." in name:
        raise ValueError("Invalid portable plugin name")
    if set(manifest) - (TEXT_FIELDS | {"$schema", "name", "keywords", "author", "extensions"}):
        raise ValueError("Unexpected portable plugin metadata")
    keywords, author = manifest.get("keywords", []), manifest.get("author", {})
    extensions = manifest.get("extensions", {})
    if (any(not isinstance(manifest.get(key, ""), str) for key in TEXT_FIELDS)
            or not isinstance(keywords, list) or not all(isinstance(word, str) for word in keywords)
            or not valid_string_map(author) or set(author) - {"name", "email", "url"}
            or not isinstance(extensions, dict)):
        raise ValueError("Invalid portable plugin metadata")

    result = {"name": name, "description": manifest.get("description", ""), "version": manifest.get("version", ""),
              "mcp_servers": [], "skills": [], "unsupported": sorted(set(extensions) | {
                  path.name for path in root.iterdir() if path.is_dir() and "." in path.name and not path.name.startswith(".")
              }), "diagnostics": []}
    skills = root / "skills"
    if skills.is_dir():
        for folder in sorted(skills.iterdir()):
            if folder.is_dir():
                try:
                    result["skills"].append(parse_skill(folder, folder.name)[0])
                except (OSError, ValueError):
                    result["diagnostics"].append({"component": "skill", "name": folder.name, "error": "invalid_skill"})
    if (root / "mcp.json").exists():
        try:
            config = read_json(root, "mcp.json")
            if (not isinstance(config, dict) or set(config) != {"$schema", "mcpServers"}
                    or config["$schema"] != SCHEMAS + "mcp.schema.json" or not isinstance(config["mcpServers"], dict)):
                raise ValueError
        except (ValueError, OSError):
            result["diagnostics"].append({"component": "mcp", "error": "invalid_mcp_config"})
        else:
            for server, data in config["mcpServers"].items():
                try:
                    result["mcp_servers"].append(component(root, server, data))
                except (ValueError, OSError):
                    result["diagnostics"].append({"component": "mcp", "name": server, "error": "invalid_server"})
    return result
