from __future__ import annotations

import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sdlc.engine import Harness
from sdlc.schema import read_json
from sdlc.workspace import write_json


def command(code: str, kind: str = "test", timeout: int = 3) -> dict:
    return {"argv": [sys.executable, "-c", code], "kind": kind,
            "timeout_seconds": timeout, "cwd": ".", "env": {}}


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="keel-test-")
        self.root = Path(self.temp.name)
        Harness.initialize(self.root)
        self.h = Harness(self.root)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.h.close)
        (self.root / "app.py").write_text("def value():\n    return 42\n")
        self.config = self.h.config()
        self.config["source_roots"] = ["app.py"]
        self.config["checks"] = {
            "unit": command("import app; from pathlib import Path; assert app.value() == 42; assert not Path('.sdlc/fail').exists(); print('behavior verified')"),
            "observe": command("import app; assert app.value() == 42; print('observed')", "observe"),
        }
        self.save_config()
        self.h.new("change", "Deliver the value 42 with observable behavior")
        self.manifest = read_json(self.h.manifest_path("change"))
        self.manifest.update({
            "owner": "test-owner", "risk": "low", "scope": {"include": ["value()"], "exclude": ["network services"], "constraints": ["offline"]},
            "requirements": [{"id": "R1", "statement": "Return 42", "acceptance": ["value() == 42"], "checks": ["unit"], "priority": "must"}],
            "slices": [{"id": "S1", "title": "Implement value", "requirements": ["R1"], "checks": ["unit"], "depends_on": [], "done_when": "R1 passes", "rollback": "Restore the previous app.py"}],
            "release": {"strategy": "Local replacement", "rollback": "Restore saved app.py", "abort_when": ["value differs from 42"],
                        "observe_checks": ["observe"], "artifacts": ["app.py"], "irreversible": False, "observation_window_seconds": 1},
        })
        self.save_manifest()

    def save_config(self):
        write_json(self.root / ".sdlc/config.json", self.config)

    def save_manifest(self):
        write_json(self.h.manifest_path("change"), self.manifest)
        return self.h.sync("change")

    def unknown(self, **overrides):
        return {"id": "U1", "question": "Is 42 the intended value?", "kind": "requirement", "impact": 2,
                "confidence": "unknown", "reversibility": "easy", "blocks": "build", "requirements": ["R1"],
                "probe": "Read and record the acceptance agreement", "check": "", "fallback": "Keep the previous value",
                "effort_minutes": 5, "owner": "test-owner", **overrides}

    def notes(self, text="Observed and reviewed the acceptance criteria against the current contract.", kind="research", scope="workspace", verdict="pass"):
        path = self.root / ".sdlc/notes.md"
        path.write_text(text)
        return self.h.attest("change", kind, ".sdlc/notes.md", "test-reviewer", scope=scope, verdict=verdict)

    def to_release(self):
        result = self.h.run("change", "unit")
        self.assertEqual(result["status"], "pass", result)
        while self.h.store.get("change")["phase"] != "release":
            self.h.advance("change")

    def deploy(self, outcome="success", packet=None):
        packet = packet or self.h.packet("change")["packet"]
        receipt = {"schema_version": 1, "packet_digest": packet["digest"], "environment": "test-local",
                   "deployment_id": "deploy-1", "outcome": outcome, "deployed_at": datetime.now(timezone.utc).isoformat(), "actor": "test-adapter"}
        write_json(self.root / ".sdlc/receipt.json", receipt)
        return self.h.receipt("change", ".sdlc/receipt.json")
