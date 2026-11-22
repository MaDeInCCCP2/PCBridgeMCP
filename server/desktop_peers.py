"""Owner-configured aliases for starting approved applications on another PCBridge."""
import re


_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")


class PeerError(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def peer_entry(settings, peer, principal):
    table = settings.get("desktop_peers")
    entry = table.get(peer) if isinstance(table, dict) and _NAME.fullmatch(peer) else None
    if not isinstance(entry, dict):
        raise PeerError("desktop_peer_unavailable", "Peer is unavailable to this client.")
    clients = entry.get("clients", [])
    if principal != "owner" and (not isinstance(clients, list) or principal not in clients):
        raise PeerError("desktop_peer_unavailable", "Peer is unavailable to this client.")
    return entry


def component(entry):
    plugin, server = entry.get("plugin"), entry.get("server")
    if not all(isinstance(value, str) and _NAME.fullmatch(value) for value in (plugin, server)):
        raise PeerError("desktop_peer_config", "Peer must identify one locally configured plugin and server.")
    return plugin, server


def resolve_peer(settings, peer, principal, source_app):
    entry = peer_entry(settings, peer, principal)
    apps = entry.get("apps")
    remote_app = apps.get(source_app) if isinstance(apps, dict) else None
    if not isinstance(remote_app, str) or not _NAME.fullmatch(remote_app):
        raise PeerError("desktop_peer_app_denied", "This application has no approved target on the peer.")
    return (*component(entry), remote_app)
