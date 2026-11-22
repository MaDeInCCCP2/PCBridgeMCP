"""SQLite admission and terminal receipts for at-most-once input tools."""
from contextlib import closing, contextmanager
import hmac
import json
import sqlite3
from time import time

from computer_use import ComputerUseError


class OperationReceiptStore:
    def __init__(self, path, secret, backend, *, scope_limit, retention=86400, clock=time):
        self.path, self.secret = path, secret
        self.table, self.error_prefix = {
            "uia": ("uia_actions", "ui"),
            "browser": ("browser_actions", "browser"),
            "desktop": ("desktop_actions", "desktop"),
        }[backend]
        self.session_scoped = backend != "uia"
        self.scope_limit, self.retention, self.clock = scope_limit, retention, clock

    def fingerprint(self, arguments):
        return hmac.digest(self.secret, json.dumps(arguments, sort_keys=True).encode(), "sha256")

    @contextmanager
    def _db(self):
        try:
            with closing(sqlite3.connect(self.path, timeout=3)) as db, db:
                db.execute(f"""CREATE TABLE IF NOT EXISTS {self.table} (
                    client TEXT, operation_id TEXT, created REAL, fingerprint BLOB, receipt TEXT,
                    PRIMARY KEY (client, operation_id)
                )""")
                db.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_created ON {self.table}(created)")
                if self.session_scoped:
                    db.execute(f"CREATE INDEX IF NOT EXISTS {self.table}_operation_scope "
                               f"ON {self.table}(operation_id, client)")
                yield db
        except sqlite3.Error as error:
            raise ComputerUseError(f"{self.error_prefix}_receipt_store_unavailable",
                                   "Input action receipt storage is unavailable.") from error

    def _find(self, db, scope, operation_id, cutoff):
        saved = db.execute(f"""SELECT fingerprint, receipt FROM {self.table}
                              WHERE client = ? AND operation_id = ? AND created >= ?""",
                           (scope, operation_id, cutoff)).fetchone()
        if saved is not None:
            fingerprint, receipt = saved
            return fingerprint, json.loads(receipt) if receipt is not None else None
        return None

    def get(self, scope, operation_id):
        if not self.path.is_file():
            return None
        with self._db() as db:
            return self._find(db, scope, operation_id, self.clock() - self.retention)

    def sessions_for(self, owner, operation_id, limit):
        if not self.path.is_file():
            return []
        # A session scope is a JSON pair [owner, session_id]. The bounds
        # select the exact serialized owner, with ',' followed by '-' in ASCII.
        prefix = "[" + json.dumps(owner)
        with self._db() as db:
            rows = db.execute(f"""SELECT client FROM {self.table}
                                  WHERE operation_id = ? AND client >= ? AND client < ? AND created >= ?
                                  ORDER BY client LIMIT ?""",
                              (operation_id, prefix + ",", prefix + "-", self.clock() - self.retention, limit))
            return [json.loads(scope)[1] for (scope,) in rows]

    def reserve(self, scope, operation_id, fingerprint):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            cutoff = now - self.retention
            db.execute(f"DELETE FROM {self.table} WHERE created < ?", (cutoff,))
            saved = self._find(db, scope, operation_id, cutoff)
            if saved is not None:
                return saved
            count = db.execute(f"SELECT COUNT(*) FROM {self.table} WHERE client = ?", (scope,)).fetchone()[0]
            if count >= self.scope_limit:
                raise ComputerUseError(f"{self.error_prefix}_operation_limit", "The retained action limit was reached.")
            db.execute(f"INSERT INTO {self.table} VALUES (?, ?, ?, ?, NULL)",
                       (scope, operation_id, now, fingerprint))
            return None

    def complete(self, scope, operation_id, receipt):
        with self._db() as db:
            db.execute(f"UPDATE {self.table} SET receipt = ? WHERE client = ? AND operation_id = ?",
                       (json.dumps(receipt), scope, operation_id))
