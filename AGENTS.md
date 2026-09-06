# Working on KEEL

This repository implements KEEL, a deterministic SDLC harness. Preserve the
separation between agent judgment, local evidence validation, and external
authorization. Read `docs/architecture.md` before changing lifecycle semantics.

## Work rules

- Preserve the user's intended outcome and existing work. Prefer bounded,
  reversible implementation steps and explain material uncertainty.
- Read the active change with `python3 -m sdlc status <id> --json` and `context`
  when `.sdlc/state.db` exists. A missing local DB is normal in a fresh clone;
  see `scripts/bootstrap.py --help` for reconstructing the example work item.
- Never patch stored evidence, verdicts, or journal hashes to turn a failure into
  success. Adjust the contract explicitly when scope or assumptions change.
- Tests must exercise observable failure modes and contracts. Use the standard
  library; do not add runtime dependencies without a concrete need.
- Execute `python3 -m unittest discover -s tests -t . -v` for behavior changes.
  Regenerate schemas after structural schema edits and check them with
  `python3 scripts/check_schemas.py`. Run the offline demo for lifecycle changes.
- Inspect argv and side effects before executing registered checks. The runner
  is not an OS sandbox, and gate readiness is not external deployment authority.
- Explicit review verdicts are operator assertions. Do not represent a
  self-review or local demo as independent approval or a production deployment.
- Keep Japanese operational documentation aligned with user-visible behavior.
  Report the actual validation environment and any material untested boundary.
- Use `rg` for focused discovery. Do not start sub-agents unless the user asks.

## Modules

`schema.py` owns structure and cross references; `workspace.py` owns input
identity; `policy.py` owns cumulative gates; `store.py` owns atomic state/history;
`runner.py` owns bounded POSIX supervision; `engine.py` coordinates them;
`cli.py` exposes text/JSON output and exit codes.
