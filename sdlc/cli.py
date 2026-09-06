"""Human-readable CLI with a stable JSON mode and explicit exit statuses."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .engine import GateBlocked, Harness
from .schema import PHASES, HarnessError, exported_schema, read_json, validate_change
from .workspace import atomic_write, contained, digest, file_hash


def parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--root", type=Path, default=argparse.SUPPRESS, help="Repository root (default: current directory)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Machine-readable output")
    root = argparse.ArgumentParser(prog="keel", description="KEEL — evidence-driven SDLC under uncertainty", parents=[common])
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True)

    def command(name, help, change=False, mutation=False):
        sub = commands.add_parser(name, help=help, parents=[common])
        if change:
            sub.add_argument("change", help="Change ID")
        if mutation:
            sub.add_argument("--expect-revision", type=int, help="Reject work based on an obsolete checkpoint")
        return sub

    init = command("init", "Initialize without overwriting existing files")
    init.add_argument("--adopt", action="store_true", help="Create a local DB for versioned config; never overwrite a DB")
    new = command("new", "Create a draft contract", True)
    new.add_argument("--intent", required=True)
    imported = command("import", "Register an existing contract as a new local change")
    imported.add_argument("file", help="Repository-relative contract JSON")
    command("list", "List changes")
    command("sync", "Validate contract changes and rewind discovery", True, True)
    command("validate", "Validate config, contract, references and dependency DAG", True)
    command("status", "Show phase, evidence freshness and budget", True)
    command("next", "Explain blockers and candidate independent work", True)
    gate = command("gate", "Evaluate cumulative readiness without changing state", True)
    gate.add_argument("--target", choices=PHASES)
    command("advance", "Cross exactly one passing phase gate", True, True)
    reopen = command("reopen", "Move backward with an audit reason", True, True)
    reopen.add_argument("--phase", choices=PHASES[:-1], required=True)
    reopen.add_argument("--reason", required=True)
    run = command("run", "Execute one trusted configured check with bounded resources", True, True)
    run.add_argument("check")
    attest = command("attest", "Import review or research notes as a named assertion", True, True)
    attest.add_argument("--kind", choices=["research", "review", "retrospective"], required=True)
    attest.add_argument("--file", required=True, help="Repository-relative UTF-8 notes")
    attest.add_argument("--actor", required=True, help="Declared author; not authenticated by this local tool")
    attest.add_argument("--scope", choices=["workspace", "contract"], default="workspace")
    attest.add_argument("--verdict", choices=["pass", "fail"], help="Required for review; defaults to pass for other notes")
    resolve = command("resolve", "Answer, refute, or temporarily accept an uncertainty", True, True)
    resolve.add_argument("uncertainty")
    resolve.add_argument("--evidence", required=True)
    resolve.add_argument("--outcome", choices=["answered", "accepted", "refuted"], required=True)
    resolve.add_argument("--answer", required=True)
    resolve.add_argument("--ttl-seconds", type=int)
    command("packet", "Prepare a verified release handoff; does not deploy", True, True)
    receipt = command("receipt", "Record an external deployment result bound to a packet", True, True)
    receipt.add_argument("--file", required=True)
    verify = command("check-packet", "Recheck an exact packet immediately before external deployment", True)
    verify.add_argument("--file", required=True)
    context = command("context", "Produce a complete Markdown handoff", True)
    context.add_argument("--out", help="Repository-relative output, preferably under .sdlc")
    command("history", "Read the change journal", True)
    recover = command("recover", "Account for a stopped, abandoned run without retrying it", True, True)
    recover.add_argument("run_id")
    recover.add_argument("--reason", required=True)
    recover.add_argument("--confirm-stopped", action="store_true")
    command("doctor", "Verify SQLite, journal replay and evidence artifacts")
    backup = command("backup", "Create a consistent database backup (copy evidence separately)")
    backup.add_argument("destination", type=Path)
    schema = command("schema", "Export the exact runtime input schema")
    schema.add_argument("kind", choices=["change", "config", "receipt"])
    return root


def dispatch(args) -> tuple[object, int]:
    root = getattr(args, "root", Path.cwd()).resolve()
    command = args.command
    if command == "schema":
        return exported_schema(args.kind), 0
    if command == "init":
        return Harness.initialize(root, adopt=args.adopt), 0
    harness = Harness(root)
    expected = getattr(args, "expect_revision", None)
    try:
        if command == "new":
            state = harness.new(args.change, args.intent)
            return {"id": state["id"], "phase": state["phase"], "revision": state["revision"],
                    "manifest": str(harness.manifest_path(args.change))}, 0
        if command == "import":
            state = harness.import_change(args.file)
            return {"id": state["id"], "phase": state["phase"], "revision": state["revision"]}, 0
        if command == "list":
            return [{"id": state["id"], "phase": state["phase"], "revision": state["revision"],
                     "intent": state["manifest"]["intent"]} for state in harness.store.all()], 0
        if command == "validate":
            manifest = read_json(harness.manifest_path(args.change))
            validate_change(manifest, harness.config())
            if manifest["id"] != args.change:
                raise HarnessError("The manifest ID does not match its directory")
            return {"valid": True, "id": args.change, "note": "Schema validity is separate from lifecycle readiness"}, 0
        if command == "sync":
            state = harness.sync(args.change, expected=expected)
        elif command == "advance":
            state = harness.advance(args.change, expected=expected)
        elif command == "reopen":
            state = harness.reopen(args.change, args.phase, args.reason, expected=expected)
        elif command == "recover":
            state = harness.recover(args.change, args.run_id, args.reason, args.confirm_stopped, expected=expected)
        else:
            state = None
        if state is not None:
            return {"id": state["id"], "phase": state["phase"], "revision": state["revision"],
                    "unchanged": state.get("unchanged", False)}, 0
        if command in {"status", "next"}:
            return harness.status(args.change), 0
        if command == "gate":
            report = harness.gate(args.change, args.target)
            return report, 0 if report["ready"] else 2
        if command == "run":
            result = harness.run(args.change, args.check, expected=expected)
            return result, 0 if result["status"] == "pass" else 4
        if command == "attest":
            return harness.attest(args.change, args.kind, args.file, args.actor, scope=args.scope, verdict=args.verdict, expected=expected), 0
        if command == "resolve":
            return harness.resolve(args.change, args.uncertainty, args.evidence, args.outcome, args.answer,
                                   ttl_seconds=args.ttl_seconds, expected=expected), 0
        if command == "packet":
            return harness.packet(args.change, expected=expected), 0
        if command == "receipt":
            return harness.receipt(args.change, args.file, expected=expected), 0
        if command == "check-packet":
            supplied = read_json(contained(root, args.file, internal=True))
            state = harness.store.get(args.change)
            if state["phase"] != "release":
                raise HarnessError("Deployment preflight requires the release phase")
            report = harness.gate(args.change, "release")
            if not report["ready"]:
                raise GateBlocked(report)
            if (supplied != state.get("packet") or supplied.get("identity") != report["identity"]
                    or supplied.get("proofs") != report["proofs"]
                    or supplied.get("resolution_digest") != report["resolution_digest"]
                    or digest({k: v for k, v in supplied.items() if k != "digest"}) != supplied.get("digest")):
                raise HarnessError("Packet is obsolete, altered, or not issued by this workspace")
            return {"ready": True, "packet_digest": supplied["digest"], "identity": report["identity"]}, 0
        if command == "context":
            text = harness.context(args.change)
            if args.out:
                destination = contained(root, args.out, internal=True)
                # Do not allow a report to overwrite the database, contract or another source input.
                if destination.suffix != ".md" or ".sdlc" not in Path(args.out).parts:
                    raise HarnessError("Context output must be a .md file under .sdlc (or use stdout redirection)")
                atomic_write(destination, text)
                return {"path": str(destination)}, 0
            return text, 0
        if command == "history":
            return harness.store.history(args.change), 0
        if command == "doctor":
            report = harness.store.audit()
            harness.config()
            checked, active = 0, []
            for state in harness.store.all():
                for item in state["evidence"]:
                    if item["status"] == "running":
                        active.append({"change": state["id"], "run": item["id"]})
                    if item.get("artifact_hash"):
                        path = contained(root, item["artifact"], internal=True)
                        if file_hash(path) != item["artifact_hash"]:
                            raise HarnessError(f"Evidence artifact modified: {item['id']}")
                        checked += 1
            return {**report, "artifacts_checked": checked, "active_runs": active}, 0
        if command == "backup":
            path = args.destination if args.destination.is_absolute() else root / args.destination
            return harness.store.backup(path.resolve()), 0
        raise HarnessError(f"Unsupported command: {command}")
    finally:
        harness.close()


def human(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "gate" in value:
        gate = value["gate"]
        lines = [f"KEEL {value['id']} · {value['phase']} · revision {value['revision']}", value["intent"],
                 f"{gate['target']}: {'READY' if gate['ready'] else 'BLOCKED'} (risk: {gate['risk']})"]
        lines.extend(f"  [{b['code']}] {b['message']}" for b in gate["blockers"])
        budget = value["budgets"]
        lines.append(f"Budget: {budget['runs_used']}/{budget['max_runs']} runs, {budget['seconds_charged']}/{budget['max_seconds']} seconds")
        lines.append("Next:")
        lines.extend(f"  {action}" for action in value["next_actions"])
        candidates = [p["id"] for p in value["work_queue"] if p["candidate_for_independent_work"]]
        if candidates:
            lines.append("Candidates for independent work (verify scope first): " + ", ".join(candidates))
        return "\n".join(lines)
    if isinstance(value, dict) and "blockers" in value:
        lines = [f"Gate → {value['target']}: {'READY' if value['ready'] else 'BLOCKED'}"]
        lines.extend(f"  [{b['code']}] {b['message']}\n    → {b['action']}" for b in value["blockers"])
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        result, status = dispatch(args)
    except GateBlocked as exc:
        result, status = exc.report, 2
    except (HarnessError, OSError, sqlite3.Error, ValueError, KeyError, RecursionError) as exc:
        result, status = {"error": str(exc), "type": type(exc).__name__}, 3
    except KeyboardInterrupt:
        result, status = {"error": "Interrupted; inspect active runs before resuming"}, 130
    if getattr(args, "json", False):
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    else:
        print(human(result))
    return status
