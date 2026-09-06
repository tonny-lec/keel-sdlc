import os
import time
from pathlib import Path
from unittest.mock import patch

from sdlc.engine import Harness
from sdlc.schema import HarnessError
from .support import WorkspaceCase, command


class RunnerTests(WorkspaceCase):
    def check(self, code, timeout=1):
        self.config["checks"]["unit"] = command(code, timeout=timeout)
        self.save_config()
        return self.h.run("change", "unit")

    def test_timeout_is_bounded_and_never_passing(self):
        started = time.monotonic()
        result = self.check("import time; time.sleep(30)")
        self.assertEqual(result["status"], "timeout")
        self.assertLess(time.monotonic() - started, 4)
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_process_that_closes_stdout_is_still_bounded(self):
        result = self.check("import os,time; os.close(1); os.close(2); time.sleep(30)")
        self.assertEqual(result["status"], "timeout")

    def test_output_flood_is_bounded(self):
        self.config["max_log_bytes"] = 1024
        result = self.check("import sys; sys.stdout.write('x'*1000000)")
        self.assertEqual(result["status"], "output_limit")
        self.assertEqual((self.root / result["artifact"]).stat().st_size, 1024)

    def test_timeout_kills_child_process_group(self):
        result = self.check("import subprocess,sys,time; from pathlib import Path; p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path('.sdlc/child.pid').write_text(str(p.pid)); time.sleep(30)")
        self.assertEqual(result["status"], "timeout")
        pid = int((self.root / ".sdlc/child.pid").read_text())
        stat = Path(f"/proc/{pid}/stat")
        if stat.exists():
            self.assertEqual(stat.read_text().split()[2], "Z", "descendant is still executing")

    def test_exited_parent_cannot_leave_pipe_open_via_child(self):
        started = time.monotonic()
        result = self.check("import subprocess,sys; subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])", timeout=3)
        self.assertEqual(result["status"], "pass")
        self.assertLess(time.monotonic() - started, 2)

    def test_source_mutation_during_check_invalidates_success(self):
        result = self.check("from pathlib import Path; Path('app.py').write_text('def value(): return 43\\n')")
        self.assertEqual(result["status"], "invalidated")
        self.assertEqual(result["returncode"], 0)

    def test_policy_mutation_during_check_invalidates_success(self):
        result = self.check("import json; from pathlib import Path; p=Path('.sdlc/config.json'); d=json.loads(p.read_text()); d['max_log_bytes']+=1; p.write_text(json.dumps(d))")
        self.assertEqual(result["status"], "invalidated")

    def test_missing_executable_is_recorded_and_charged_as_attempt(self):
        self.config["checks"]["unit"]["argv"] = ["keel-missing-program-7e852"]
        self.save_config()
        result = self.h.run("change", "unit")
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.h.status("change")["budgets"]["runs_used"], 1)

    def test_shell_metacharacters_are_not_interpreted(self):
        self.config["checks"]["unit"]["argv"] = ["/bin/echo", "$(touch INJECTED); > INJECTED"]
        self.save_config()
        result = self.h.run("change", "unit")
        self.assertEqual(result["status"], "pass")
        self.assertFalse((self.root / "INJECTED").exists())

    def test_secrets_not_in_allowlist_are_not_inherited(self):
        with patch.dict(os.environ, {"KEEL_TEST_SECRET": "never-print-this"}):
            result = self.check("import os; assert 'KEEL_TEST_SECRET' not in os.environ; print('clean')")
        self.assertEqual(result["status"], "pass")
        self.assertNotIn("never-print-this", (self.root / result["artifact"]).read_text())

    def test_relative_path_executable_identity_is_resolved_from_check_cwd(self):
        binary = self.root / "bin/tool"
        binary.parent.mkdir()
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)
        self.config["checks"]["unit"].update(argv=["tool"], env={"PATH": "bin"})
        self.save_config()
        self.assertEqual(self.h.run("change", "unit")["status"], "pass")
        self.assertTrue(self.h.gate("change", "release")["ready"])
        binary.write_text("#!/bin/sh\nexit 1\n")
        self.assertFalse(self.h.gate("change", "release")["ready"])

    def test_attempt_and_total_run_budgets_persist_across_sync(self):
        self.manifest["budgets"]["max_attempts_per_check"] = 1
        self.save_manifest()
        self.h.run("change", "unit")
        self.manifest["intent"] += " (clarified)"
        self.save_manifest()
        with self.assertRaisesRegex(HarnessError, "Attempt budget"):
            self.h.run("change", "unit")
        self.manifest["budgets"].update(max_attempts_per_check=2, max_runs=1)
        self.save_manifest()
        with self.assertRaisesRegex(HarnessError, "budget exhausted"):
            self.h.run("change", "observe")

    def test_time_budget_limits_command_timeout(self):
        self.manifest["budgets"]["max_seconds"] = 1
        self.save_manifest()
        result = self.check("import time; time.sleep(30)", timeout=10)
        self.assertEqual(result["timeout_seconds"], 1)
        with self.assertRaisesRegex(HarnessError, "budget exhausted"):
            self.h.run("change", "observe")

    def test_run_reservation_prevents_concurrent_runs_across_changes(self):
        self.h.new("another", "Independent change in the same workspace")

        def reserved(*args, **kwargs):
            other = Harness(self.root)
            try:
                with self.assertRaisesRegex(HarnessError, "is active"):
                    other.run("another", "unit")
                with self.assertRaisesRegex(HarnessError, "is active"):
                    other.advance("change")
            finally:
                other.close()
            log = args[4]
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text("stubbed runner boundary")
            return {"status": "pass", "reason": "test", "returncode": 0, "duration_seconds": .01, "log_bytes": 23}

        with patch("sdlc.engine.execute", side_effect=reserved):
            result = self.h.run("change", "unit")
            self.assertEqual(result["status"], "pass", result)

    def test_abandoned_run_requires_explicit_stopped_confirmation_and_keeps_charge(self):
        def crash(*args, **kwargs):
            raise SystemExit("simulated supervisor termination")
        with patch("sdlc.engine.execute", side_effect=crash), self.assertRaises(SystemExit):
            self.h.run("change", "unit")
        run = self.h.store.get("change")["evidence"][-1]
        with self.assertRaisesRegex(HarnessError, "confirm-stopped"):
            self.h.recover("change", run["id"], "Interrupted", False)
        with self.assertRaisesRegex(HarnessError, "still alive"):
            self.h.recover("change", run["id"], "Interrupted", True)
        self.h.store.mutate("change", "test_owner_disappeared", lambda state: state["evidence"][-1].update(owner_pid=99999999))
        self.h.recover("change", run["id"], "Verified that process tree has stopped", True)
        recovered = self.h.store.get("change")["evidence"][-1]
        self.assertEqual(recovered["status"], "abandoned")
        self.assertEqual(recovered["charged_seconds"], run["timeout_seconds"])

    def test_symlinks_in_source_artifacts_and_managed_paths_are_rejected(self):
        target = self.root / "target.py"
        target.write_text("pass")
        (self.root / "app.py").unlink()
        (self.root / "app.py").symlink_to(target)
        with self.assertRaisesRegex(HarnessError, "Symlink"):
            self.h.run("change", "unit")
