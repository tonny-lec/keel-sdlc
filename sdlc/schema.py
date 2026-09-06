"""One declarative schema drives both runtime validation and JSON Schema export.

Structural validation is deliberately separate from lifecycle readiness: a draft
can have empty sections, but it cannot silently introduce unknown fields or IDs.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import PurePosixPath
from typing import Any

PHASES = ("discover", "define", "plan", "build", "verify", "release", "observe", "closed")
KINDS = ("experiment", "test", "lint", "security", "review", "rollback", "observe")
ID_PATTERN = r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
SLUG_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"


class HarnessError(Exception):
    """An actionable input, state, or policy failure."""


def string(*, blank: bool = False, pattern: str | None = None) -> dict:
    spec = {"type": "string", "minLength": 0 if blank else 1, "maxLength": 16000}
    if pattern:
        spec["pattern"] = pattern
    return spec


def enum(*values: str) -> dict:
    return {"type": "string", "enum": list(values)}


def integer(low: int, high: int) -> dict:
    return {"type": "integer", "minimum": low, "maximum": high}


def array(items: dict, minimum: int = 0) -> dict:
    return {"type": "array", "items": items, "minItems": minimum, "maxItems": 1000}


def obj(**fields: dict) -> dict:
    return {"type": "object", "properties": fields, "required": list(fields), "additionalProperties": False}


ID = string(pattern=ID_PATTERN)
IDS = array(ID)
TEXTS = array(string())
REVERSIBILITY = enum("easy", "costly", "irreversible")

CHANGE_SCHEMA = obj(
    schema_version={"type": "integer", "const": 1},
    id=string(pattern=SLUG_PATTERN),
    intent=string(),
    owner=string(blank=True),
    risk=enum("low", "medium", "high"),
    scope=obj(include=TEXTS, exclude=TEXTS, constraints=TEXTS),
    requirements=array(obj(
        id=ID, statement=string(), acceptance=TEXTS, checks=IDS,
        priority=enum("must", "should"),
    )),
    uncertainties=array(obj(
        id=ID, question=string(), kind=enum("requirement", "technical", "environment", "dependency"), impact=integer(1, 5),
        confidence=enum("unknown", "partial", "supported"),
        reversibility=REVERSIBILITY, blocks=enum(*PHASES[1:]),
        requirements=IDS, probe=string(blank=True), check=string(blank=True),
        fallback=string(blank=True), effort_minutes=integer(1, 1440), owner=string(blank=True),
    )),
    decisions=array(obj(
        id=ID, question=string(), choice=string(), alternatives=TEXTS,
        rationale=string(), reversibility=REVERSIBILITY,
        rollback=string(blank=True), uncertainties=IDS,
    )),
    slices=array(obj(
        id=ID, title=string(), requirements=IDS, checks=IDS, depends_on=IDS,
        done_when=string(), rollback=string(blank=True),
    )),
    release=obj(
        strategy=string(blank=True), rollback=string(blank=True), abort_when=TEXTS,
        observe_checks=IDS, artifacts=TEXTS, irreversible={"type": "boolean"},
        observation_window_seconds=integer(1, 604800),
    ),
    budgets=obj(max_runs=integer(1, 10000), max_seconds=integer(1, 604800),
                max_attempts_per_check=integer(1, 1000)),
)

CHECK_SCHEMA = obj(
    argv=array(string(), 1), kind=enum(*KINDS), timeout_seconds=integer(1, 86400),
    cwd=string(), env={"type": "object", "additionalProperties": string(blank=True)},
)

CONFIG_SCHEMA = obj(
    schema_version={"type": "integer", "const": 1},
    source_roots=array(string(), 1), exclude_dirs=TEXTS,
    evidence_ttl_seconds=integer(1, 2592000), max_log_bytes=integer(1024, 10485760),
    env_allowlist=TEXTS,
    checks={"type": "object", "propertyNames": {"pattern": ID_PATTERN},
            "additionalProperties": CHECK_SCHEMA},
)

RECEIPT_SCHEMA = obj(
    schema_version={"type": "integer", "const": 1},
    packet_digest=string(pattern=r"^[a-f0-9]{64}$"),
    environment=string(), deployment_id=string(), outcome=enum("success", "failure"),
    deployed_at=string(), actor=string(),
)


def validate(value: Any, spec: dict, path: str = "$") -> None:
    types = {"object": dict, "array": list, "string": str, "integer": int, "boolean": bool}
    expected = spec.get("type")
    if expected and type(value) is not types[expected]:
        raise HarnessError(f"{path}: expected {expected}, got {type(value).__name__}")
    if "const" in spec and value != spec["const"]:
        raise HarnessError(f"{path}: unsupported value/version {value!r}")
    if "enum" in spec and value not in spec["enum"]:
        raise HarnessError(f"{path}: must be one of {spec['enum']}")
    if expected == "object":
        fields = spec.get("properties", {})
        missing = set(spec.get("required", [])) - value.keys()
        if missing:
            raise HarnessError(f"{path}: missing fields {sorted(missing)}")
        for key, item in value.items():
            if "propertyNames" in spec:
                validate(key, {"type": "string", **spec["propertyNames"]}, f"{path}.{key}")
            if key in fields:
                validate(item, fields[key], f"{path}.{key}")
            elif spec.get("additionalProperties") is False:
                raise HarnessError(f"{path}: unknown field {key!r}")
            elif isinstance(spec.get("additionalProperties"), dict):
                validate(item, spec["additionalProperties"], f"{path}.{key}")
    elif expected == "array":
        if not spec.get("minItems", 0) <= len(value) <= spec.get("maxItems", 10000):
            raise HarnessError(f"{path}: invalid number of items")
        for index, item in enumerate(value):
            validate(item, spec["items"], f"{path}[{index}]")
    elif expected == "string":
        if "\x00" in value or not spec.get("minLength", 0) <= len(value) <= spec.get("maxLength", 16000):
            raise HarnessError(f"{path}: invalid string length or NUL byte")
        if spec.get("minLength", 0) and not value.strip():
            raise HarnessError(f"{path}: must not be whitespace")
        if "pattern" in spec and not re.fullmatch(spec["pattern"], value):
            raise HarnessError(f"{path}: invalid identifier or format")
    elif expected == "integer":
        if not spec.get("minimum", -math.inf) <= value <= spec.get("maximum", math.inf):
            raise HarnessError(f"{path}: value out of range")


def relative_path(value: str, *, allow_internal: bool = False) -> None:
    path = PurePosixPath(value)
    if (not value or "\\" in value or "\x00" in value or path.is_absolute()
            or ".." in path.parts or ":" in value
            or (not allow_internal and any(p in {".git", ".sdlc"} for p in path.parts))):
        raise HarnessError(f"Unsafe repository-relative path: {value!r}")


def validate_config(config: dict) -> None:
    validate(config, CONFIG_SCHEMA)
    for root in config["source_roots"]:
        relative_path(root)
    for directory in config["exclude_dirs"]:
        if directory in {".", ".."} or "/" in directory or "\\" in directory:
            raise HarnessError("exclude_dirs must contain directory basenames")
    for name in config["env_allowlist"]:
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise HarnessError(f"Invalid environment variable name: {name}")
    for check in config["checks"].values():
        relative_path(check["cwd"])
        for name in check["env"]:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise HarnessError(f"Invalid environment variable name: {name}")


def validate_change(change: dict, config: dict) -> None:
    validate(change, CHANGE_SCHEMA)
    ids: dict[str, set[str]] = {}
    for section in ("requirements", "uncertainties", "decisions", "slices"):
        values = [entry["id"] for entry in change[section]]
        if len(values) != len(set(values)):
            raise HarnessError(f"Duplicate ID in {section}")
        ids[section] = set(values)
    ids["checks"] = set(config["checks"])

    def refs(values: list, section: str, location: str) -> None:
        if len(values) != len(set(values)):
            raise HarnessError(f"{location}: duplicate references")
        unknown = set(values) - ids[section]
        if unknown:
            raise HarnessError(f"{location}: unknown {section}: {sorted(unknown)}")

    for requirement in change["requirements"]:
        refs(requirement["checks"], "checks", requirement["id"])
    for unknown in change["uncertainties"]:
        refs(unknown["requirements"], "requirements", unknown["id"])
        if unknown["check"]:
            refs([unknown["check"]], "checks", unknown["id"])
    for decision in change["decisions"]:
        refs(decision["uncertainties"], "uncertainties", decision["id"])
    for part in change["slices"]:
        for section in ("requirements", "checks"):
            refs(part[section], section, part["id"])
        refs(part["depends_on"], "slices", part["id"])
    refs(change["release"]["observe_checks"], "checks", "release.observe_checks")
    for check in change["release"]["observe_checks"]:
        if config["checks"][check]["kind"] != "observe":
            raise HarnessError(f"{check}: observation check must have kind=observe")
    for path in change["release"]["artifacts"]:
        relative_path(path)
    if change["release"]["observation_window_seconds"] >= config["evidence_ttl_seconds"]:
        raise HarnessError("Observation window must be shorter than evidence TTL")
    graph = {part["id"]: part["depends_on"] for part in change["slices"]}
    # Kahn's algorithm avoids recursion limits on otherwise valid long plans.
    remaining = {node: len(dependencies) for node, dependencies in graph.items()}
    dependents = {node: [] for node in graph}
    for node, dependencies in graph.items():
        for dependency in dependencies:
            dependents[dependency].append(node)
    ready = [node for node, count in remaining.items() if not count]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for dependent in dependents[node]:
            remaining[dependent] -= 1
            if remaining[dependent] == 0:
                ready.append(dependent)
    if visited != len(graph):
        raise HarnessError("Dependency cycle in implementation slices")


def read_json(path) -> dict:
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise HarnessError(f"{path}: duplicate JSON key {key!r}")
            result[key] = value
        return result

    def bad_constant(value):
        raise HarnessError(f"{path}: non-finite JSON number {value}")

    try:
        if path.stat().st_size > 2_000_000:
            raise HarnessError(f"{path}: JSON document exceeds 2 MB")
        return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                          parse_constant=bad_constant)
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise HarnessError(f"Cannot read JSON {path}: {exc}") from exc


def exported_schema(kind: str) -> dict:
    return {"$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": f"KEEL {kind} v1",
            **{"change": CHANGE_SCHEMA, "config": CONFIG_SCHEMA, "receipt": RECEIPT_SCHEMA}[kind]}


def default_config() -> dict:
    return {
        "schema_version": 1, "source_roots": ["."],
        "exclude_dirs": ["__pycache__", ".venv", "node_modules", ".pytest_cache", "build", "dist"],
        "evidence_ttl_seconds": 86400, "max_log_bytes": 1048576,
        "env_allowlist": ["PATH", "LANG", "LC_ALL", "TZ", "SYSTEMROOT", "WINDIR", "PATHEXT", "TMPDIR", "TEMP", "TMP"],
        "checks": {},
    }


def draft(slug: str, intent: str) -> dict:
    return {
        "schema_version": 1, "id": slug, "intent": intent, "owner": "", "risk": "medium",
        "scope": {"include": [], "exclude": [], "constraints": []},
        "requirements": [], "uncertainties": [], "decisions": [], "slices": [],
        "release": {"strategy": "", "rollback": "", "abort_when": [],
                    "observe_checks": [], "artifacts": [], "irreversible": False, "observation_window_seconds": 60},
        "budgets": {"max_runs": 30, "max_seconds": 1800, "max_attempts_per_check": 5},
    }
