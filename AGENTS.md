# Hermes Lab agent instructions

_Root-folder harness contract for AI coding and research agents_

---

## What this repo is

A generic autoresearch lab scaffold. The repo holds code and templates. The data root holds canonical experiment state.

- `python3 scripts/labctl.py` is the control surface.
- `LAB_MANIFEST.json` is the machine-readable command and contract reference.
- `README.md` has the full user-facing docs (templates, search strategies, examples).

## Data root vs workspace

- **Data root** (`$HERMES_LAB_DATA_ROOT`, default `./lab-data`): Lab state -- experiments, runs, metrics, dispatch queue.
- **Workspace** (`workspace_root` in SPEC): Your experiment code. Can be any path on disk.

## Repo ingress

Read these in order when pointed at the repo root:

1. `README.md`
2. `LAB_MANIFEST.json`
3. `docs/AUTORESEARCH-COMPATIBILITY.md`
4. `docs/REFERENCE-EXECUTOR.md`
5. `docs/MULTI-FIDELITY.md`
6. `docs/LOCAL-AGENT-MUTATION.md`
7. `docs/DISPATCH.md`

If a lab data root already exists, then read:

1. `README-FIRST.md`
2. `LAB-STATUS.md`
3. `LAB-INDEX.json`
4. `PROGRAM.md`
5. `experiments/<id>/RUNBOOK.md`
6. `experiments/<id>/SUMMARY.md`
7. `experiments/<id>/NEXT.md`

## Write rules

- Treat the data root as canonical state.
- Only write inside a claimed run bundle when executing an experiment.
- If working from `dispatch/running/<dispatch-id>/`, only write inside `run/`.
- Do not hand-edit derived files.
- `RESULT.md` is the write-ahead artifact.
- Use the runbook and spec to determine mutable vs read-only surfaces.

## Dispatch protocol

For agent-managed execution, use the dispatch protocol instead of writing directly into `experiments/<id>/runs/`. The queue package includes `dispatch.json`, an `input/` snapshot, and a writable `run/` folder that is ingested into canonical history only after completion.

If `mutation_command` is omitted and `agent_provider` is set, the lab synthesizes a local mutation worker through `scripts/local_agent_mutation.py`.

See `docs/DISPATCH.md` for the full protocol.
