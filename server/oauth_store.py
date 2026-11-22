"""Persistent OAuth registrations and hashed, revocable bearer tokens."""
import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class OAuthStore:
    def __init__(self, path, root_token, clock=time.time):
        self.path = path
        self.clock = clock
        # Rotating the bridge secret also invalidates previously issued OAuth tokens.
        self.token_key = hashlib.sha256(("pcbridge-oauth:" + root_token).encode()).digest()
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS clients (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, uris TEXT NOT NULL,
                    auth_method TEXT NOT NULL, secret_hash TEXT
                );
                CREATE TABLE IF NOT EXISTS tokens (
                    hash TEXT PRIMARY KEY, client_id TEXT NOT NULL, resource TEXT NOT NULL,
                    scope TEXT NOT NULL, expires REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS tokens_client ON tokens(client_id);
            """)

    @contextmanager
    def db(self):
        with closing(sqlite3.connect(self.path)) as db, db:
            yield db

    def register(self, name, uris, method):
        client_id = "pcbridge-" + secrets.token_hex(16)
        secret = secrets.token_urlsafe(32) if method != "none" else None
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if db.execute("SELECT COUNT(*) FROM clients").fetchone()[0] >= 1024:
                return None
            db.execute("INSERT INTO clients VALUES (?, ?, ?, ?, ?)",
                       (client_id, name, json.dumps(uris), method, digest(secret) if secret else None))
        return client_id, secret

    def client(self, client_id):
        with self.db() as db:
            row = db.execute("SELECT name, uris, auth_method, secret_hash FROM clients WHERE id = ?",
                             (client_id,)).fetchone()
        if row:
            return {"name": row[0], "uris": json.loads(row[1]), "method": row[2], "secret_hash": row[3]}
        return None

    def _token_hash(self, token):
        return hmac.new(self.token_key, token.encode(), "sha256").hexdigest()

    def issue(self, client_id, resource, scope, ttl):
        token = "pcb_at_" + secrets.token_urlsafe(32)
        now = self.clock()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if client_id != "pcbridge" and not db.execute(
                    "SELECT 1 FROM clients WHERE id = ?", (client_id,)).fetchone():
                return None
            db.execute("DELETE FROM tokens WHERE expires <= ?", (now,))
            # Keep token storage bounded without revoking the client's concurrent active sessions.
            if db.execute("SELECT COUNT(*) FROM tokens WHERE client_id = ?", (client_id,)).fetchone()[0] >= 64:
                return None
            db.execute("INSERT INTO tokens VALUES (?, ?, ?, ?, ?)",
                       (self._token_hash(token), client_id, resource, scope, now + ttl))
        return token

    def authenticate(self, token, resource, scope="mcp"):
        if not token.startswith("pcb_at_") or len(token) != 50:
            return None
        with self.db() as db:
            row = db.execute(
                "SELECT client_id FROM tokens WHERE hash = ? AND resource = ? AND scope = ? AND expires > ?",
                (self._token_hash(token), resource, scope, self.clock())).fetchone()
        return row[0] if row else None

    def revoke(self, token, client_id):
        with self.db() as db:
            db.execute("DELETE FROM tokens WHERE hash = ? AND client_id = ?", (self._token_hash(token), client_id))

    def remove_client(self, client_id):
        with self.db() as db:
            db.execute("DELETE FROM tokens WHERE client_id = ?", (client_id,))
            return db.execute("DELETE FROM clients WHERE id = ?", (client_id,)).rowcount

    def clients(self):
        with self.db() as db:
            return db.execute("SELECT id, name FROM clients ORDER BY id").fetchall()
