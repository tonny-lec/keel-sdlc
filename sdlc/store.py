"""SQLite transactions + a hash-linked, replayable state journal.

This detects accidental corruption, not forgery by someone who can rewrite the
database. Backups/externally anchored journal heads belong to the operator.
"""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .schema import HarnessError
from .workspace import canonical, digest

ZERO = "0" * 64


class Store:
    def __init__(self, path: Path, *, create: bool = False):
        if not create and not path.is_file():
            raise HarnessError("Workspace is not initialized. Run: python3 -m sdlc init")
        self.path = path
        self.db = sqlite3.connect(f"{path.as_uri()}?mode={'rwc' if create else 'rw'}", uri=True, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA busy_timeout=10000")
        self.db.execute("PRAGMA synchronous=FULL")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in (0, 1) or (version == 0 and not create):
            self.close()
            raise HarnessError(f"Unsupported database schema version: {version}")
        if create and version == 0:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.executescript("""
                BEGIN IMMEDIATE;
                CREATE TABLE changes (id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL);
                CREATE TABLE events (
                    seq INTEGER PRIMARY KEY, change_id TEXT NOT NULL REFERENCES changes(id),
                    revision INTEGER NOT NULL, at REAL NOT NULL, kind TEXT NOT NULL,
                    details TEXT NOT NULL, state TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL,
                    UNIQUE(change_id, revision)
                );
                PRAGMA user_version=1;
                COMMIT;
            """)

    def close(self):
        self.db.close()

    @contextmanager
    def transaction(self):
        try:
            self.db.execute("BEGIN IMMEDIATE")
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self, slug: str) -> dict:
        row = self.db.execute("SELECT * FROM changes WHERE id=?", (slug,)).fetchone()
        if row is None:
            raise HarnessError(f"Unknown change: {slug}")
        event = self.db.execute("SELECT state, revision FROM events WHERE change_id=? ORDER BY seq DESC LIMIT 1", (slug,)).fetchone()
        if not event or event["state"] != row["state"] or event["revision"] != row["revision"]:
            raise HarnessError("State/journal mismatch. Run doctor and restore a verified backup.")
        state = json.loads(row["state"])
        if state["revision"] != row["revision"]:
            raise HarnessError("State revision mismatch")
        return state

    def all(self) -> list[dict]:
        return [self.get(row[0]) for row in self.db.execute("SELECT id FROM changes ORDER BY id")]

    def create(self, state: dict) -> dict:
        with self.transaction():
            if self.db.execute("SELECT 1 FROM changes WHERE id=?", (state["id"],)).fetchone():
                raise HarnessError(f"Change already exists: {state['id']}")
            state["revision"] = 1
            self.db.execute("INSERT INTO changes VALUES (?,?,?)", (state["id"], 1, canonical(state)))
            self._event(state, "created", {})
        return state

    def mutate(self, slug: str, kind: str, callback, *, expected: int | None = None, details: dict | None = None) -> dict:
        with self.transaction():
            state = self.get(slug)
            if expected is not None and state["revision"] != expected:
                raise HarnessError(f"Revision conflict: expected {expected}, actual {state['revision']}; refresh context")
            if callback(state) is False:
                return state
            state["revision"] += 1
            self.db.execute("UPDATE changes SET revision=?, state=? WHERE id=?",
                            (state["revision"], canonical(state), slug))
            self._event(state, kind, details or {})
        return state

    def _event(self, state: dict, kind: str, details: dict):
        last = self.db.execute("SELECT seq, hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        event = {"seq": last["seq"] + 1 if last else 1, "change_id": state["id"],
                 "revision": state["revision"], "at": time.time(), "kind": kind,
                 "details": details, "state": state, "prev_hash": last["hash"] if last else ZERO}
        self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?,?)", (
            event["seq"], event["change_id"], event["revision"], event["at"], kind,
            canonical(details), canonical(state), event["prev_hash"], digest(event),
        ))

    def audit(self) -> dict:
        # A read transaction gives the journal and projection one consistent view.
        with self.db:
            self.db.execute("BEGIN")
            if self.db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise HarnessError("SQLite integrity check failed")
            previous, count, states = ZERO, 0, {}
            for row in self.db.execute("SELECT * FROM events ORDER BY seq"):
                event = dict(row)
                claimed = event.pop("hash")
                event["details"] = json.loads(event["details"])
                event["state"] = json.loads(event["state"])
                count += 1
                prior_revision = states.get(row["change_id"], {}).get("revision", 0)
                if (event["seq"] != count or event["prev_hash"] != previous or digest(event) != claimed
                        or event["revision"] != prior_revision + 1
                        or event["state"]["id"] != row["change_id"]
                        or event["state"]["revision"] != event["revision"]):
                    raise HarnessError(f"Journal integrity failure at event {row['seq']}")
                states[row["change_id"]] = event["state"]
                previous = claimed
            current = {state["id"]: state for state in self.all()}
            if states != current:
                raise HarnessError("Replayed journal differs from current state")
        return {"ok": True, "events": count, "changes": len(states), "journal_head": previous}

    def history(self, slug: str) -> list[dict]:
        self.get(slug)
        return [{**dict(row), "details": json.loads(row["details"])} for row in self.db.execute(
            "SELECT seq, revision, at, kind, details, hash FROM events WHERE change_id=? ORDER BY seq", (slug,))]

    def backup(self, destination: Path) -> dict:
        self.audit()
        if destination.exists():
            raise HarnessError("Backup destination already exists; choose a new file")
        # Exclusive creation prevents replacing another process's backup.
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.touch(exist_ok=False)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
        return {"backup": str(destination), "includes": "database only; copy .sdlc/evidence and contracts separately"}
