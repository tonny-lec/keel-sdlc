"""An offline, real red→green→release→observe exercise in an isolated directory.

The product-owner answer and deployment are explicitly local demo events. No
model, remote provider, credentials, or repository under development is used.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from sdlc.engine import Harness
from sdlc.schema import read_json
from sdlc.workspace import atomic_write, write_json

GOOD = '''import math

def normalize_timeout(value):
    if value is None:
        return 30.0
    if isinstance(value, bool):
        raise ValueError("A timeout must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError("A timeout must be a positive finite number")
    return number
'''

TESTS = '''import unittest
from app import normalize_timeout

class TimeoutTests(unittest.TestCase):
    def test_default(self):
        self.assertEqual(normalize_timeout(None), 30.0)

    def test_explicit_positive_value(self):
        self.assertEqual(normalize_timeout("2.5"), 2.5)

    def test_invalid_values(self):
        for value in (0, -1, "0", float("inf"), float("nan"), True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_timeout(value)

if __name__ == "__main__":
    unittest.main()
'''


def run_demo(root: Path) -> dict:
    Harness.initialize(root)
    harness = Harness(root)
    steps = []

    def record(step, **details):
        steps.append({"step": step, **details})

    def notes(name, text):
        relative = f".sdlc/{name}.md"
        atomic_write(root / relative, text)
        return relative

    try:
        (root / "app.py").write_text("def normalize_timeout(value):\n    return 30.0 if value is None else float(value)\n")
        (root / "test_app.py").write_text(TESTS)
        config = harness.config()
        config["source_roots"] = ["app.py", "test_app.py"]
        config["checks"] = {
            "unit": {"argv": [sys.executable, "-m", "unittest", "test_app", "-v"], "kind": "test", "timeout_seconds": 10, "cwd": ".", "env": {}},
            "health": {"argv": [sys.executable, "-c", "import runpy; f=runpy.run_path('.sdlc/deployed/app.py')['normalize_timeout']; assert f(None)==30; assert f('2.5')==2.5; print('local deployed artifact responds correctly')"],
                       "kind": "observe", "timeout_seconds": 10, "cwd": ".", "env": {}},
        }
        write_json(root / ".sdlc/config.json", config)
        harness.new("timeout", "Clarify and implement a safe timeout contract")
        manifest = read_json(harness.manifest_path("timeout"))
        manifest.update({
            "owner": "demo-product-owner (simulated)", "risk": "medium",
            "scope": {"include": ["Normalize one timeout value"], "exclude": ["Networking", "Retries", "Production deployment"], "constraints": ["No external dependencies"]},
            "requirements": [
                {"id": "R1", "statement": "Omitted timeout defaults to 30 seconds", "acceptance": ["None returns 30.0"], "checks": ["unit"], "priority": "must"},
                {"id": "R2", "statement": "Explicit timeout is a positive finite number", "acceptance": ["2.5 and '2.5' return 2.5", "Zero, negatives, NaN, infinity and booleans raise ValueError"], "checks": ["unit"], "priority": "must"},
            ],
            "uncertainties": [{"id": "U1", "question": "Does zero disable the timeout?", "kind": "requirement", "impact": 3,
                               "confidence": "unknown", "reversibility": "easy", "blocks": "build", "requirements": ["R2"],
                               "probe": "Obtain the owner's explicit zero-value semantics", "check": "", "fallback": "Keep the old interface until semantics are agreed",
                               "effort_minutes": 5, "owner": "demo-product-owner (simulated)"}],
            "decisions": [{"id": "D1", "question": "Implicit timeout disabling?", "choice": "Reject zero",
                           "alternatives": ["Zero disables timeout", "A separate disable flag"], "rationale": "An accidental zero must not create an unbounded wait",
                           "reversibility": "easy", "rollback": "Restore the preceding module", "uncertainties": ["U1"]}],
            "slices": [{"id": "S1", "title": "Default and validate the timeout", "requirements": ["R1", "R2"], "checks": ["unit"], "depends_on": [],
                        "done_when": "All declared timeout cases pass", "rollback": "Restore the previous app.py from the local backup"}],
            "release": {"strategy": "Copy the verified module to an isolated local deployment directory", "rollback": "Restore .sdlc/deployed/previous.py",
                        "abort_when": ["Health check fails"], "observe_checks": ["health"], "artifacts": ["app.py"], "irreversible": False,
                        "observation_window_seconds": 1},
        })
        write_json(harness.manifest_path("timeout"), manifest)
        harness.sync("timeout")
        assert not harness.gate("timeout", "build")["ready"]
        record("ambiguity_blocks_build", uncertainty="U1")
        note = notes("answer", "Demo fixture answer, not a real stakeholder approval: zero is invalid. A future disable option must be separate.\n")
        answer = harness.attest("timeout", "research", note, "simulated-demo-owner", scope="contract")
        harness.resolve("timeout", "U1", answer["id"], "answered", "Zero is invalid; absence defaults to 30 seconds")
        while harness.store.get("timeout")["phase"] != "build":
            harness.advance("timeout")
        red = harness.run("timeout", "unit")
        assert red["status"] == "fail", red
        assert not harness.gate("timeout", "release")["ready"]
        record("failing_implementation_blocked", evidence=red["id"])
        (root / "app.py").write_text(GOOD)
        green = harness.run("timeout", "unit")
        assert green["status"] == "pass", green
        record("corrected_implementation_verified", evidence=green["id"])
        note = notes("review", "Demo self-review: exercised default, positive string conversion, zero, negative, NaN, infinity and boolean inputs. Checked the local rollback and observation path. This is not an independent human approval.\n")
        harness.attest("timeout", "review", note, "demo-self-review", verdict="pass")
        harness.advance("timeout")
        harness.advance("timeout")
        packet = harness.packet("timeout")["packet"]
        deployment = root / ".sdlc/deployed"
        deployment.mkdir()
        (deployment / "previous.py").write_text("def normalize_timeout(value): return 30.0\n")
        shutil.copyfile(root / "app.py", deployment / "app.py")
        receipt = {"schema_version": 1, "packet_digest": packet["digest"], "environment": "isolated-local-demo",
                   "deployment_id": "local-copy-1", "outcome": "success", "deployed_at": datetime.now(timezone.utc).isoformat(), "actor": "local-demo-adapter"}
        write_json(root / ".sdlc/receipt.json", receipt)
        harness.receipt("timeout", ".sdlc/receipt.json")
        harness.advance("timeout")
        record("local_artifact_deployed", packet_digest=packet["digest"])
        time.sleep(1.02)
        observed = harness.run("timeout", "health")
        assert observed["status"] == "pass", observed
        note = notes("retrospective", "Zero-value ambiguity was resolved before completion. The regression test caught the initial permissive implementation. The local deployed artifact passed checks after the declared observation window. Production rollout and continuous monitoring were outside this demo.\n")
        harness.attest("timeout", "retrospective", note, "demo-self-review")
        harness.advance("timeout")
        context = root / ".sdlc/changes/timeout/context.md"
        atomic_write(context, harness.context("timeout"))
        audit = harness.store.audit()
        record("closed_with_observation_and_lessons", events=audit["events"])
        return {"ok": True, "root": str(root), "phase": "closed", "steps": steps,
                "context": str(context), "journal_head": audit["journal_head"]}
    finally:
        harness.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="New directory for the isolated demo")
    args = parser.parse_args()
    if args.output:
        if args.output.exists():
            parser.error("--output must not already exist")
        root = args.output.resolve()
        root.mkdir(parents=True)
    else:
        root = Path(tempfile.mkdtemp(prefix="keel-demo-"))
    print(json.dumps(run_demo(root), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
