import copy
import json
import random

from sdlc.schema import HarnessError, read_json, relative_path, validate_change, validate_config
from .support import WorkspaceCase


class SchemaTests(WorkspaceCase):
    def test_draft_is_valid_but_not_ready(self):
        self.h.new("draft", "An ambiguous request")
        self.assertFalse(self.h.gate("draft")["ready"])

    def test_unknown_fields_and_numeric_type_confusion_rejected(self):
        for edit in (lambda m: m.update(typo=True), lambda m: m.update(schema_version=True),
                     lambda m: m["budgets"].update(max_runs=1.5), lambda m: m.update(schema_version=2)):
            with self.subTest(edit=edit):
                manifest = copy.deepcopy(self.manifest)
                edit(manifest)
                with self.assertRaises(HarnessError):
                    validate_change(manifest, self.config)

    def test_unknown_reference_duplicate_id_and_cycle_rejected(self):
        broken = copy.deepcopy(self.manifest)
        broken["requirements"][0]["checks"] = ["missing"]
        with self.assertRaisesRegex(HarnessError, "unknown"):
            validate_change(broken, self.config)
        broken = copy.deepcopy(self.manifest)
        broken["requirements"] *= 2
        with self.assertRaisesRegex(HarnessError, "Duplicate"):
            validate_change(broken, self.config)
        broken = copy.deepcopy(self.manifest)
        broken["slices"][0]["depends_on"] = ["S1"]
        with self.assertRaisesRegex(HarnessError, "cycle"):
            validate_change(broken, self.config)

    def test_generated_dags_and_back_edges(self):
        randomizer = random.Random(1949)
        for size in range(2, 35):
            manifest = copy.deepcopy(self.manifest)
            manifest["slices"] = [{**copy.deepcopy(manifest["slices"][0]), "id": f"S{i}",
                                   "depends_on": [f"S{j}" for j in range(i) if randomizer.random() < .2]} for i in range(size)]
            validate_change(manifest, self.config)
            manifest["slices"][0]["depends_on"] = [f"S{size - 1}"]
            manifest["slices"][-1]["depends_on"] = ["S0"]
            with self.assertRaisesRegex(HarnessError, "cycle"):
                validate_change(manifest, self.config)

    def test_path_traversal_and_external_paths_rejected(self):
        for path in ("../secret", "/etc/passwd", "a/../../b", "C:\\secret", ".sdlc/state.db", "a\\b", ".git/config"):
            with self.subTest(path=path), self.assertRaises(HarnessError):
                relative_path(path)

    def test_maximum_length_linear_plan_does_not_hit_python_recursion_limit(self):
        manifest = copy.deepcopy(self.manifest)
        manifest["slices"] = [{**self.manifest["slices"][0], "id": f"S{i}",
                               "depends_on": [f"S{i-1}"] if i else []} for i in range(1000)]
        validate_change(manifest, self.config)

    def test_config_uses_argv_and_valid_env_names(self):
        self.config["checks"]["unit"]["argv"] = "python3 -c 'pass'"
        with self.assertRaises(HarnessError):
            validate_config(self.config)
        self.config["checks"]["unit"]["argv"] = ["python3"]
        self.config["checks"]["unit"]["env"] = {"A=B": "bad"}
        with self.assertRaises(HarnessError):
            validate_config(self.config)

    def test_json_duplicate_keys_nan_and_non_utf8_rejected(self):
        path = self.root / ".sdlc/invalid.json"
        for value in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff'):
            path.write_bytes(value)
            with self.assertRaises(HarnessError):
                read_json(path)

    def test_cannot_claim_lint_as_behavior_verification(self):
        self.config["checks"]["unit"]["kind"] = "lint"
        self.save_config()
        self.assertIn("behavior_test", [b["code"] for b in self.h.gate("change", "build")["blockers"]])

    def test_should_requirement_cannot_silently_escape_traceability(self):
        self.manifest["requirements"].append({"id": "R2", "statement": "Extra promise", "acceptance": ["observable"], "checks": ["unit"], "priority": "should"})
        self.save_manifest()
        self.assertIn("traceability", [b["code"] for b in self.h.gate("change", "build")["blockers"]])

    def test_observation_cannot_outlive_all_evidence(self):
        self.manifest["release"]["observation_window_seconds"] = self.config["evidence_ttl_seconds"]
        with self.assertRaisesRegex(HarnessError, "Observation window"):
            self.save_manifest()
