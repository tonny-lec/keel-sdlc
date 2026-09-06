"""Deterministic lifecycle gates and uncertainty triage.

No model confidence score can substitute for a current, relevant evidence item.
All readiness checks are cumulative. A missing input is a blocker, not a pass.
"""

from __future__ import annotations

import time
from pathlib import Path

from .schema import PHASES, HarnessError
from .workspace import contained, digest, file_hash


def identity(manifest: dict, config: dict, source: dict) -> dict:
    return {"contract": digest(manifest), "config": digest(config), "source": source["digest"], "runtime": source["runtime"]}


def latest(state: dict, *, check: str | None = None, kind: str | None = None) -> dict | None:
    return next((item for item in reversed(state["evidence"])
                 if (check is None or item.get("check") == check)
                 and (kind is None or item["kind"] == kind)), None)


def freshness(item: dict | None, current: dict, config: dict, root: Path, now: float) -> list[str]:
    if item is None:
        return ["missing"]
    problems = []
    if item["status"] != "pass":
        problems.append(item["status"])
    for field in item.get("bindings", ("contract", "config", "source", "runtime")):
        if item["identity"][field] != current[field]:
            problems.append(f"{field}_changed")
    finished = item.get("finished_at")
    if finished is None:
        problems.append("unfinished")
    elif finished > now + 1:
        problems.append("clock_in_future")
    elif now - finished > config["evidence_ttl_seconds"]:
        problems.append("expired")
    if item.get("artifact"):
        try:
            path = contained(root, item["artifact"], internal=True)
            if file_hash(path) != item.get("artifact_hash"):
                problems.append("artifact_changed")
        except (HarnessError, OSError):
            problems.append("artifact_missing_or_unsafe")
    elif item["status"] == "pass":
        problems.append("artifact_missing")
    return problems


def effective_risk(manifest: dict) -> str:
    if (manifest["release"]["irreversible"]
            or any(u["impact"] >= 4 or u["reversibility"] == "irreversible" for u in manifest["uncertainties"])
            or any(d["reversibility"] == "irreversible" for d in manifest["decisions"])):
        return "high"
    return manifest["risk"]


def uncertainty_status(unknown: dict, state: dict, current: dict, config: dict, root: Path, now: float) -> tuple[str, list[str]]:
    resolution = state["resolutions"].get(unknown["id"])
    if resolution is None:
        return "open", ["no recorded answer"]
    evidence = next((e for e in state["evidence"] if e["id"] == resolution["evidence"]), None)
    problems = freshness(evidence, current, config, root, now)
    # A subsequent failed/changed probe cannot be hidden behind an older answer.
    if evidence and evidence.get("check") and latest(state, check=evidence["check"])["id"] != evidence["id"]:
        problems.append("probe_superseded")
    if resolution["expires_at"] <= now:
        problems.append("answer_expired")
    if resolution["outcome"] == "refuted" and evidence and evidence["identity"]["contract"] == current["contract"]:
        return "refuted", problems
    if problems:
        return "stale", problems
    return resolution["outcome"], []


def uncertainty_queue(manifest: dict, state: dict, current: dict, config: dict, root: Path, now: float) -> list[dict]:
    result = []
    for unknown in manifest["uncertainties"]:
        status, problems = uncertainty_status(unknown, state, current, config, root, now)
        if status in {"answered", "accepted"}:
            continue
        exposure = unknown["impact"] * {"unknown": 3, "partial": 2, "supported": 1}[unknown["confidence"]]
        exposure *= {"easy": 1, "costly": 2, "irreversible": 3}[unknown["reversibility"]]
        due = PHASES.index(unknown["blocks"])
        if unknown["impact"] >= 4 or unknown["reversibility"] == "irreversible":
            due = min(due, PHASES.index("build"))
        result.append({**unknown, "status": status, "problems": problems,
                       "effective_blocks": PHASES[min(due, PHASES.index("release"))],
                       "exposure": exposure, "priority_per_minute": round(exposure / unknown["effort_minutes"], 4)})
    return sorted(result, key=lambda u: (PHASES.index(u["effective_blocks"]), -u["priority_per_minute"], u["id"]))


def evaluate(state: dict, config: dict, source: dict, root: Path, target: str, *, now: float | None = None) -> dict:
    if target not in PHASES:
        raise HarnessError(f"Unknown phase: {target}")
    now = time.time() if now is None else now
    manifest = state["manifest"]
    current = identity(manifest, config, source)
    level = PHASES.index(target)
    risk = effective_risk(manifest)
    blockers, proofs = [], []
    resolution_digest = digest({u["id"]: state["resolutions"].get(u["id"]) for u in manifest["uncertainties"]})

    def block(code: str, message: str, action: str):
        blockers.append({"code": code, "message": message, "action": action})

    def need(condition, code: str, message: str, action: str):
        if not condition:
            block(code, message, action)

    def evidence(check: str | None = None, kind: str | None = None, after: float | None = None):
        item = latest(state, check=check, kind=kind)
        problems = freshness(item, current, config, root, now)
        if item and after is not None and item.get("started_at", 0) < after:
            problems.append("predates_deployment")
        label = f"check:{check}" if check else f"attestation:{kind}"
        if problems:
            block("evidence", f"{label}: {', '.join(problems)}",
                  f"run {manifest['id']} {check}" if check else f"attest {manifest['id']} --kind {kind} --file <notes> --actor <reviewer> --verdict <pass|fail>")
        else:
            proofs.append(item["id"])
        return item

    if level >= 1:
        need(manifest["owner"].strip(), "owner", "A decision owner is required", "Fill owner and sync")
        need(manifest["scope"]["include"], "scope", "Included scope is empty", "Define the smallest useful outcome and sync")
        need(manifest["scope"]["exclude"], "scope", "Excluded scope is empty", "Make non-goals explicit and sync")
        need(manifest["requirements"], "requirements", "No requirements recorded", "Add observable acceptance criteria and sync")
        for requirement in manifest["requirements"]:
            need(requirement["acceptance"], "acceptance", f"{requirement['id']}: no acceptance criteria", "Describe a falsifiable outcome and sync")

    queue = uncertainty_queue(manifest, state, current, config, root, now)
    for unknown in manifest["uncertainties"]:
        status, _ = uncertainty_status(unknown, state, current, config, root, now)
        if status in {"answered", "accepted"}:
            proofs.append(state["resolutions"][unknown["id"]]["evidence"])
    for unknown in queue:
        if unknown["status"] == "refuted" or level >= PHASES.index(unknown["effective_blocks"]):
            block("uncertainty", f"{unknown['id']}: {unknown['status']} — {unknown['question']}",
                  "Revise the contradicted premise and sync" if unknown["status"] == "refuted"
                  else f"{unknown['probe'] or 'Define the smallest discriminating probe'}; then resolve {manifest['id']} {unknown['id']}")

    if level >= 2:
        for unknown in manifest["uncertainties"]:
            need(unknown["owner"].strip() and unknown["probe"].strip() and unknown["fallback"].strip(),
                 "probe", f"{unknown['id']}: owner, probe and fallback are required", "Complete the uncertainty record and sync")
        if risk != "low":
            need(manifest["decisions"], "decision", "Record the material design decision", "Compare alternatives, state rationale, then sync")
        for decision in manifest["decisions"]:
            need(decision["alternatives"], "alternatives", f"{decision['id']}: no alternative considered", "Record at least one alternative and sync")
            need(decision["rollback"].strip(), "decision_rollback", f"{decision['id']}: recovery/compensation is missing", "Record the reversal or compensation plan and sync")

    if level >= 3:
        need(manifest["slices"], "slices", "No bounded implementation slices", "Create dependency-ordered slices and sync")
        for part in manifest["slices"]:
            need(part["requirements"] and part["checks"] and part["rollback"].strip(),
                 "slice_contract", f"{part['id']}: requirement, checks and rollback are required", "Complete the slice contract and sync")
        for requirement in manifest["requirements"]:
            linked = [part for part in manifest["slices"] if requirement["id"] in part["requirements"]]
            # All declared requirements are promises; should means prioritization, not an invisible waiver.
            need(linked, "traceability", f"{requirement['id']}: no implementation slice", "Link a slice or explicitly remove/defer this requirement and sync")
            need(requirement["checks"], "test_mapping", f"{requirement['id']}: no executable check", "Map a relevant test and sync")
            covered_checks = {name for part in linked for name in part["checks"]}
            need(set(requirement["checks"]) <= covered_checks, "traceability",
                 f"{requirement['id']}: slice checks do not cover requirement checks", "Repair the requirement → slice → check mapping and sync")
            need(any(config["checks"][name]["kind"] == "test" for name in requirement["checks"]),
                 "behavior_test", f"{requirement['id']}: at least one behavioral test is required", "Configure a kind=test check that exercises the acceptance criteria")

    if level >= 4:
        need(source["files"], "source", "Source scope contains no files", "Set source_roots to the implementation and its inputs")

    if level >= 5:
        required = {check for part in manifest["slices"] for check in part["checks"]}
        required.update(check for requirement in manifest["requirements"] for check in requirement["checks"])
        if risk == "high":
            for kind in ("security", "rollback"):
                names = [name for name, check in config["checks"].items() if check["kind"] == kind]
                need(names, "risk_check", f"High risk requires a {kind} check", f"Configure and run a kind={kind} check")
                required.update(names)
        for name in sorted(required):
            evidence(check=name)
        if risk != "low":
            evidence(kind="review")
        release = manifest["release"]
        for field in ("strategy", "rollback", "abort_when", "observe_checks", "artifacts"):
            need(release[field] and (not isinstance(release[field], str) or release[field].strip()),
                 "release_plan", f"release.{field} is required", "Complete the rollout, abort and recovery plan and sync")
        for path, sha in source["artifacts"].items():
            need(sha, "release_artifact", f"Release artifact is missing: {path}", "Build the artifact, then rerun verification against it")

    if level >= 6:
        packet = state.get("packet")
        need(packet and packet["identity"] == current, "packet", "A current release packet is required", f"packet {manifest['id']}")
        if packet:
            need(packet["proofs"] == sorted(set(proofs)), "packet_proofs", "Release proofs changed after packet preparation", f"packet {manifest['id']} and obtain a new deployment receipt")
            need(packet.get("resolution_digest") == resolution_digest, "packet_resolutions", "Recorded answers changed after packet preparation", f"packet {manifest['id']} and obtain a new deployment receipt")
        deployment = state.get("deployment")
        need(deployment and deployment["receipt"]["outcome"] == "success", "deployment", "No successful deployment receipt", f"receipt {manifest['id']} --file <receipt.json>")
        if deployment:
            receipt = deployment["receipt"]
            need(packet and receipt["packet_digest"] == packet["digest"], "deployment_binding",
                 "Deployment receipt is bound to a different release packet", "Use the receipt for the current packet")
            evidence(kind="deployment")

    if level >= 7:
        deployment = state.get("deployment")
        after = (deployment["recorded_at"] if deployment else now) + manifest["release"]["observation_window_seconds"]
        need(now >= after, "observation_window", "The observation window has not elapsed", "Wait for the declared observation window, then run its checks")
        for name in manifest["release"]["observe_checks"]:
            evidence(check=name, after=after)
        evidence(kind="retrospective", after=after)

    return {"target": target, "ready": not blockers, "risk": risk,
            "blockers": blockers, "proofs": sorted(set(proofs)), "identity": current,
            "uncertainties": queue, "resolution_digest": resolution_digest}
