import contextlib
import io
import json
import subprocess
import sys
import tempfile
from pathlib import Path

from sdlc.cli import main
from sdlc.engine import Harness
from sdlc.schema import HarnessError, exported_schema
from sdlc.workspace import write_json
from .support import WorkspaceCase


class CliTests(WorkspaceCase):
    def call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["--root", str(self.root), *args, "--json"])
        return code, json.loads(output.getvalue())

    def test_json_exit_codes_distinguish_blocker_error_and_check_failure(self):
        self.assertEqual(self.call("status", "change")[0], 0)
        self.assertEqual(self.call("gate", "change", "--target", "release")[0], 2)
        self.assertEqual(self.call("status", "missing")[0], 3)
        (self.root / ".sdlc/fail").touch()
        self.assertEqual(self.call("run", "change", "unit")[0], 4)

    def test_context_round_trips_in_json_and_writes_only_internal_markdown(self):
        code, text = self.call("context", "change")
        self.assertEqual(code, 0)
        self.assertIn("Traceability", text)
        self.assertIn("R1", text)
        self.assertEqual(self.call("context", "change", "--out", "app.py")[0], 3)
        code, result = self.call("context", "change", "--out", ".sdlc/changes/change/context.md")
        self.assertEqual(code, 0)
        self.assertEqual(Path(result["path"]).read_text(), text)

    def test_initialization_is_non_destructive(self):
        before = (self.root / ".sdlc/config.json").read_bytes()
        self.assertEqual(self.call("init")[0], 3)
        self.assertEqual(before, (self.root / ".sdlc/config.json").read_bytes())
        self.assertEqual(self.call("init", "--adopt")[0], 3)

    def test_cloned_contract_can_be_adopted_without_inventing_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            write_json(root / ".sdlc/config.json", self.config)
            write_json(root / ".sdlc/changes/change/change.json", self.manifest)
            Harness.initialize(root, adopt=True)
            adopted = Harness(root)
            try:
                state = adopted.import_change(".sdlc/changes/change/change.json")
                self.assertEqual(state["phase"], "discover")
                self.assertEqual(state["evidence"], [])
                with self.assertRaises(HarnessError):
                    adopted.import_change(".sdlc/changes/change/change.json")
            finally:
                adopted.close()

    def test_check_packet_rejects_file_tampering_and_input_drift(self):
        self.to_release()
        packet = self.h.packet("change")
        relative = ".sdlc/changes/change/packet.json"
        self.assertEqual(self.call("check-packet", "change", "--file", relative)[0], 0)
        path = Path(packet["path"])
        value = json.loads(path.read_text())
        value["risk"] = "invented"
        path.write_text(json.dumps(value))
        self.assertEqual(self.call("check-packet", "change", "--file", relative)[0], 3)

    def test_doctor_verifies_artifact_hashes(self):
        evidence = self.h.run("change", "unit")
        code, report = self.call("doctor")
        self.assertEqual(code, 0)
        self.assertEqual(report["artifacts_checked"], 1)
        (self.root / evidence["artifact"]).write_text("altered")
        self.assertEqual(self.call("doctor")[0], 3)

    def test_exported_schema_uses_runtime_contract(self):
        for kind in ("change", "config", "receipt"):
            code, result = self.call("schema", kind)
            self.assertEqual(code, 0)
            self.assertEqual(result, exported_schema(kind))

    def test_real_module_entrypoint(self):
        completed = subprocess.run([sys.executable, "-m", "sdlc", "--root", str(self.root), "status", "change", "--json"],
                                   cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=10)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["id"], "change")
