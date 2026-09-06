import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

from sdlc.schema import HarnessError
from sdlc.store import Store
from .support import WorkspaceCase


class StoreTests(WorkspaceCase):
    def test_replay_matches_projection_and_backup(self):
        self.h.advance("change")
        self.h.run("change", "unit")
        report = self.h.store.audit()
        self.assertGreater(report["events"], 3)
        target = self.root / ".sdlc/backup.db"
        self.h.store.backup(target)
        backup = Store(target)
        try:
            self.assertEqual(backup.audit(), report)
            self.assertEqual(backup.get("change"), self.h.store.get("change"))
        finally:
            backup.close()
        with self.assertRaises(HarnessError):
            self.h.store.backup(target)

    def test_mutation_exception_rolls_back_state_and_event(self):
        before = self.h.store.audit()
        state = self.h.store.get("change")
        def broken(current):
            current["phase"] = "closed"
            raise RuntimeError("fault during transition")
        with self.assertRaises(RuntimeError):
            self.h.store.mutate("change", "bad_transition", broken)
        self.assertEqual(self.h.store.audit(), before)
        self.assertEqual(self.h.store.get("change"), state)

    def test_modified_history_is_detected(self):
        self.h.advance("change")
        with self.h.store.db:
            self.h.store.db.execute("UPDATE events SET details='{}x' WHERE seq=1")
        with self.assertRaises((HarnessError, json.JSONDecodeError)):
            self.h.store.audit()

    def test_modified_projection_is_detected_on_read(self):
        with self.h.store.db:
            self.h.store.db.execute("UPDATE changes SET state='{}' WHERE id='change'")
        with self.assertRaisesRegex(HarnessError, "mismatch"):
            self.h.store.get("change")

    def test_two_writers_with_same_revision_have_one_winner(self):
        revision = self.h.store.get("change")["revision"]
        barrier = threading.Barrier(2)
        path = self.h.store.path
        def attempt(number):
            store = Store(path)
            try:
                barrier.wait(timeout=5)
                try:
                    store.mutate("change", "concurrent", lambda state: state.update(winner=number), expected=revision)
                    return "won"
                except HarnessError as exc:
                    self.assertIn("Revision conflict", str(exc))
                    return "conflict"
            finally:
                store.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(attempt, [1, 2])), ["won", "conflict"])
        self.assertEqual(self.h.store.get("change")["revision"], revision + 1)
        self.assertTrue(self.h.store.audit()["ok"])

    def test_unknown_database_version_is_not_silently_migrated(self):
        path = self.root / ".sdlc/future.db"
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA user_version=99")
        with self.assertRaisesRegex(HarnessError, "Unsupported"):
            Store(path)
