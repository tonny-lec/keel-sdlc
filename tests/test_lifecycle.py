import copy
import os
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sdlc.engine import GateBlocked
from sdlc.policy import evaluate, identity
from sdlc.schema import HarnessError, PHASES, read_json
from sdlc.workspace import write_json
from .support import WorkspaceCase, command


class LifecycleTests(WorkspaceCase):
    def test_complete_lifecycle_requires_real_post_release_observation(self):
        self.to_release()
        self.assertFalse(self.h.gate("change")["ready"])
        self.deploy()
        self.h.advance("change")
        self.assertFalse(self.h.gate("change")["ready"])
        self.h.run("change", "observe")
        self.notes(kind="retrospective")
        time.sleep(1.02)
        # Earlier samples do not become valid simply because the clock advanced.
        self.assertFalse(self.h.gate("change")["ready"])
        self.h.run("change", "observe")
        self.notes(kind="retrospective")
        self.assertTrue(self.h.gate("change")["ready"])
        self.assertEqual(self.h.advance("change")["phase"], "closed")
        with self.assertRaises(HarnessError):
            self.h.run("change", "unit")
        self.assertTrue(self.h.store.audit()["ok"])

    def test_no_phase_skipping_and_blocked_advance_does_not_mutate(self):
        self.h.new("empty", "Build something")
        before = self.h.store.get("empty")
        with self.assertRaises(GateBlocked):
            self.h.advance("empty")
        self.assertEqual(before, self.h.store.get("empty"))
        self.assertEqual(self.h.advance("change")["phase"], "define")

    def test_contract_drift_requires_sync_and_rewinds(self):
        self.to_release()
        self.manifest["intent"] = "Deliver a revised requirement"
        write_json(self.h.manifest_path("change"), self.manifest)
        with self.assertRaisesRegex(HarnessError, "Unsynced"):
            self.h.gate("change")
        self.assertEqual(self.h.sync("change")["phase"], "discover")
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_formatting_only_edit_is_not_a_contract_change(self):
        revision = self.h.store.get("change")["revision"]
        self.h.manifest_path("change").write_text(__import__("json").dumps(self.manifest))
        self.assertTrue(self.h.sync("change")["unchanged"])
        self.assertEqual(revision, self.h.store.get("change")["revision"])

    def test_new_failure_masks_an_old_success(self):
        self.assertEqual(self.h.run("change", "unit")["status"], "pass")
        self.assertTrue(self.h.gate("change", "release")["ready"])
        (self.root / ".sdlc/fail").touch()
        self.assertEqual(self.h.run("change", "unit")["status"], "fail")
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_source_and_executable_mode_changes_invalidate_evidence(self):
        self.h.run("change", "unit")
        path = self.root / "app.py"
        os.utime(path, None)
        self.assertTrue(self.h.gate("change", "release")["ready"])
        path.chmod(path.stat().st_mode | 0o100)
        self.assertFalse(self.h.gate("change", "release")["ready"])
        path.chmod(path.stat().st_mode & ~0o111)
        path.write_text("def value(): return 43\n")
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_policy_changes_invalidate_evidence(self):
        self.h.run("change", "unit")
        self.config["max_log_bytes"] += 1
        self.save_config()
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_runtime_environment_changes_invalidate_evidence(self):
        self.config["env_allowlist"].append("KEEL_TEST_INPUT")
        self.save_config()
        with patch.dict(os.environ, {"KEEL_TEST_INPUT": "first"}):
            self.h.run("change", "unit")
            self.assertTrue(self.h.gate("change", "release")["ready"])
        with patch.dict(os.environ, {"KEEL_TEST_INPUT": "second"}):
            self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_expired_or_future_dated_evidence_blocks(self):
        self.h.run("change", "unit")
        state, config, source = self.h.inputs("change")
        for now in (time.time() + config["evidence_ttl_seconds"] + 1, time.time() - 60):
            self.assertFalse(evaluate(state, config, source, self.root, "release", now=now)["ready"])

    def test_missing_or_modified_evidence_blocks(self):
        result = self.h.run("change", "unit")
        path = self.root / result["artifact"]
        path.write_text("forged log")
        self.assertFalse(self.h.gate("change", "release")["ready"])
        path.unlink()
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_artifact_outside_source_roots_is_bound(self):
        path = self.root / "package.bin"
        path.write_bytes(b"v1")
        self.manifest["release"]["artifacts"] = ["package.bin"]
        self.save_manifest()
        self.h.run("change", "unit")
        self.assertTrue(self.h.gate("change", "release")["ready"])
        path.write_bytes(b"v2")
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_unknown_can_block_one_slice_but_surface_independent_work(self):
        self.manifest["uncertainties"] = [self.unknown()]
        self.manifest["requirements"].append({"id": "R2", "statement": "Independent work", "acceptance": ["Defined outcome"], "checks": ["unit"], "priority": "must"})
        self.manifest["slices"].append({**self.manifest["slices"][0], "id": "S2", "requirements": ["R2"]})
        self.save_manifest()
        status = self.h.status("change")
        self.assertFalse(status["work_queue"][0]["candidate_for_independent_work"])
        self.assertTrue(status["work_queue"][1]["candidate_for_independent_work"])

    def test_requirement_answer_survives_source_edit_when_contract_scoped(self):
        self.manifest["uncertainties"] = [self.unknown()]
        self.save_manifest()
        self.assertFalse(self.h.gate("change", "build")["ready"])
        item = self.notes(scope="contract")
        self.h.resolve("change", "U1", item["id"], "answered", "The owner confirmed 42")
        (self.root / "app.py").write_text("def value():\n    return 42  # implementation detail\n")
        self.assertTrue(self.h.gate("change", "build")["ready"])

    def test_technical_answer_cannot_use_contract_only_evidence(self):
        self.manifest["uncertainties"] = [self.unknown(kind="technical")]
        self.save_manifest()
        item = self.notes(scope="contract")
        with self.assertRaisesRegex(HarnessError, "workspace-scoped"):
            self.h.resolve("change", "U1", item["id"], "answered", "It works")
        with self.assertRaises(HarnessError):
            self.notes(kind="review", scope="contract")

    def test_configured_probe_cannot_be_replaced_with_a_note(self):
        self.manifest["uncertainties"] = [self.unknown(kind="technical", check="unit")]
        self.save_manifest()
        item = self.notes()
        with self.assertRaisesRegex(HarnessError, "requires evidence from"):
            self.h.resolve("change", "U1", item["id"], "answered", "Expected behavior")
        result = self.h.run("change", "unit")
        self.h.resolve("change", "U1", result["id"], "answered", "Experiment confirms behavior")
        (self.root / ".sdlc/fail").touch()
        self.h.run("change", "unit")
        self.assertFalse(self.h.gate("change", "build")["ready"])

    def test_high_impact_uncertainty_cannot_be_deferred_or_accepted(self):
        self.manifest["uncertainties"] = [self.unknown(impact=5, blocks="closed")]
        self.save_manifest()
        report = self.h.gate("change", "build")
        self.assertEqual(report["risk"], "high")
        self.assertIn("uncertainty", [b["code"] for b in report["blockers"]])
        item = self.notes()
        with self.assertRaisesRegex(HarnessError, "cannot be accepted"):
            self.h.resolve("change", "U1", item["id"], "accepted", "Proceed anyway")

    def test_low_impact_acceptance_expires_and_refutation_blocks(self):
        self.manifest["uncertainties"] = [self.unknown()]
        self.save_manifest()
        item = self.notes()
        self.h.resolve("change", "U1", item["id"], "accepted", "Bounded fallback is adequate", ttl_seconds=1)
        self.assertTrue(self.h.gate("change", "build")["ready"])
        state, config, source = self.h.inputs("change")
        self.assertFalse(evaluate(state, config, source, self.root, "build", now=time.time() + 2)["ready"])
        self.h.resolve("change", "U1", item["id"], "refuted", "The premise is false")
        self.assertFalse(self.h.gate("change", "define")["ready"])
        state, config, source = self.h.inputs("change")
        self.assertFalse(evaluate(state, config, source, self.root, "define", now=time.time() + 999999)["ready"])

    def test_rejected_review_supersedes_an_earlier_approval(self):
        self.manifest["risk"] = "medium"
        self.manifest["decisions"] = [{"id": "D1", "question": "Approach", "choice": "Small module", "alternatives": ["Framework"],
                                       "rationale": "Limited scope", "reversibility": "easy", "rollback": "Restore module", "uncertainties": []}]
        self.save_manifest()
        self.h.run("change", "unit")
        self.notes(kind="review")
        self.assertTrue(self.h.gate("change", "release")["ready"])
        self.notes("The reviewer found an unresolved defect.", kind="review", verdict="fail")
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_review_import_requires_an_explicit_verdict(self):
        (self.root / ".sdlc/notes.md").write_text("A review report")
        with self.assertRaisesRegex(HarnessError, "explicit --verdict"):
            self.h.attest("change", "review", ".sdlc/notes.md", "reviewer")

    def test_high_risk_needs_review_security_and_rollback_execution(self):
        self.manifest["risk"] = "high"
        self.manifest["decisions"] = [{"id": "D1", "question": "Storage choice", "choice": "Local", "alternatives": ["Remote"],
                                       "rationale": "No network needed", "reversibility": "easy", "rollback": "Restore file", "uncertainties": []}]
        self.save_manifest()
        self.h.run("change", "unit")
        self.assertFalse(self.h.gate("change", "release")["ready"])
        self.config["checks"].update({"security": command("import app; assert type(app.value()) is int", "security"),
                                      "rollback": command("import app; old=app.value; app.value=lambda:0; app.value=old; assert app.value()==42", "rollback")})
        self.save_config()
        for name in ("unit", "security", "rollback"):
            self.h.run("change", name)
        self.assertFalse(self.h.gate("change", "release")["ready"])
        self.notes(kind="review")
        self.assertTrue(self.h.gate("change", "release")["ready"])

    def test_failed_deployment_never_advances(self):
        self.to_release()
        self.deploy(outcome="failure")
        with self.assertRaises(GateBlocked):
            self.h.advance("change")

    def test_repeated_packet_and_receipt_are_idempotent(self):
        self.to_release()
        first = self.h.packet("change")
        revision = self.h.store.get("change")["revision"]
        self.assertEqual(first, self.h.packet("change"))
        self.assertEqual(revision, self.h.store.get("change")["revision"])
        deployment = self.deploy(packet=first["packet"])
        revision = self.h.store.get("change")["revision"]
        files = set((self.root / ".sdlc/evidence").iterdir())
        self.assertEqual(deployment, self.h.receipt("change", ".sdlc/receipt.json"))
        self.assertEqual(revision, self.h.store.get("change")["revision"])
        self.assertEqual(files, set((self.root / ".sdlc/evidence").iterdir()))
        self.h.packet("change")
        self.assertEqual(deployment, self.h.store.get("change")["deployment"])

    def test_receipt_must_match_packet_and_current_proofs(self):
        self.to_release()
        packet = self.h.packet("change")["packet"]
        self.h.run("change", "unit")
        with self.assertRaisesRegex(HarnessError, "does not match"):
            self.deploy(packet=packet)

    def test_reinterpreting_the_same_evidence_invalidates_the_packet(self):
        self.manifest["uncertainties"] = [self.unknown()]
        self.save_manifest()
        item = self.notes(scope="contract")
        self.h.resolve("change", "U1", item["id"], "answered", "First answer")
        self.to_release()
        packet = self.h.packet("change")["packet"]
        self.assertIn(item["id"], packet["proofs"])
        self.h.resolve("change", "U1", item["id"], "answered", "A revised interpretation")
        with self.assertRaisesRegex(HarnessError, "does not match"):
            self.deploy(packet=packet)

    def test_receipt_cannot_predate_packet_or_be_timezone_naive(self):
        self.to_release()
        packet = self.h.packet("change")["packet"]
        for date in ((datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "2026-01-01T00:00:00"):
            receipt = {"schema_version": 1, "packet_digest": packet["digest"], "environment": "test", "deployment_id": "id", "outcome": "success", "deployed_at": date, "actor": "test"}
            write_json(self.root / ".sdlc/receipt.json", receipt)
            with self.assertRaises(HarnessError):
                self.h.receipt("change", ".sdlc/receipt.json")

    def test_optimistic_revision_rejects_stale_agent_action(self):
        revision = self.h.store.get("change")["revision"]
        self.h.advance("change", expected=revision)
        with self.assertRaisesRegex(HarnessError, "Revision conflict"):
            self.h.advance("change", expected=revision)

    def test_reopen_requires_backward_move_and_reason(self):
        self.to_release()
        with self.assertRaises(HarnessError):
            self.h.reopen("change", "observe", "skip")
        with self.assertRaises(HarnessError):
            self.h.reopen("change", "build", " ")
        self.assertEqual(self.h.reopen("change", "build", "A new defect was found")["phase"], "build")
