# Hermes Lab Architecture

## Core Idea

Hermes Lab is a file-first experiment notebook with a small scheduler.

The canonical state lives on disk in the data root:

- experiment specs
- immutable run bundles
- dispatch packages for non-canonical staging
- an append-only event ledger
- derived markdown projections for cold-start continuation
- repo-root harness guidance in `AGENTS.md` and `LAB_MANIFEST.json`

Chat is the control plane. Disk is truth.

## Canonical vs Derived

Canonical:

- `registry/events.jsonl`
- `control/inbox/*.json`
- `experiments/<id>/SPEC.yaml`
- `experiments/<id>/runs/<run-id>/manifest.json`
- `experiments/<id>/runs/<run-id>/RESULT.md`
- `experiments/<id>/runs/<run-id>/metrics.json`

Derived:

- `experiments/<id>/STATUS.json`
- `experiments/<id>/RUNBOOK.md`
- `experiments/<id>/SUMMARY.md`
- `experiments/<id>/NEXT.md`
- `experiments/<id>/context.md`
- `experiments/<id>/best.md`
- `experiments/<id>/metrics.jsonl`
- `experiments/<id>/checkpoints/*.txt`
- `LAB-STATUS.md`
- `LAB-INDEX.json`
- daily and weekly digests

Staged but not canonical until ingest:

- `dispatch/<stage>/<dispatch-id>/dispatch.json`
- `dispatch/<stage>/<dispatch-id>/input/*`
- `dispatch/<stage>/<dispatch-id>/run/*`

## Experiment Lifecycle

1. `create` writes a stable `experiments/<id>/` folder.
2. Scheduler selects eligible experiments by phase, cadence, and priority.
3. Scheduler acquires a lease in `registry/locks/<id>.lock`.
4. Scheduler creates a run bundle in `experiments/<id>/runs/`.
5. Worker logic runs inside the bundle.
6. `RESULT.md` and `metrics.json` are sealed.
7. Reducer rebuilds `RUNBOOK.md`, experiment projections, and checkpoint pointers from the run bundles.
8. Lease is released.

Dispatch lifecycle:

1. `dispatch-ready` snapshots context and prepares `dispatch/ready/<dispatch-id>/`.
2. `dispatch-claim` moves a package to `dispatch/running/`.
3. A worker writes only inside `run/`.
4. `dispatch-complete` seals the package.
5. `dispatch-ingest` copies the sealed run into canonical experiment history.
6. Lease is released and projections are rebuilt.

Phases used by the implementation:

- `queued`
- `active`
- `awaiting-human`
- `paused`
- `completed`

## Ingress Sequence

Fresh agents should read:

1. `AGENTS.md` and `LAB_MANIFEST.json` if they start from the repo root
2. `LAB-STATUS.md`
3. `LAB-INDEX.json`
4. `PROGRAM.md`
5. `experiments/<id>/RUNBOOK.md`
6. `experiments/<id>/SUMMARY.md`
7. `experiments/<id>/NEXT.md`
8. `experiments/<id>/SPEC.yaml` if they need more detail

`README-FIRST.md` and `AGENT_ENTRY.md` exist at the root as the ingress card.

## Safety Properties

- Data root path is validated at startup to prevent accidental writes to wrong locations.
- Leases use atomic lock directories.
- Run bundles are sealed before reduction.
- `LAB-STATUS.md` and projections are rebuildable from canonical run bundles.
- The built-in watchdog can reclaim stale leases and flag stale status files.

## Execution Model

The repo currently supports two execution modes:

1. Built-in stub execution
   Use this while wiring the lab. It produces sealed run bundles and keeps the reducer, digests, and lease path working.

2. `executor_command`
   Add an executable path in the SPEC to run a real worker inside each run bundle.

Generalized autoresearch fields such as `workspace_root`, `mutable_paths`, `read_only_paths`, `validation_command`, and `acceptance_rule` let the same scheduler host very different experiment types without changing the lab core.

The shipped `scripts/reference_executor.py` is the first concrete adapter. It runs mutations in a sandbox, evaluates a metric, records a decision artifact, and can optionally apply a winning patch back to the original git workspace.

For local model-backed mutation, the preferred abstraction is now the generic `agent_provider` contract. The lab synthesizes `scripts/local_agent_mutation.py`, which then routes to a concrete provider adapter without changing the core scheduler/executor path. Custom adapters are auto-discovered from `scripts/<provider>_mutation_adapter.py`.

The lab now also supports multi-fidelity execution inside one experiment. A single experiment can move between tiers such as `proxy`, `validation`, and `final`, with tier-specific command overrides and executor classes. This keeps cheap screening runs and expensive finalist runs in one stable experiment history instead of splitting them into unrelated tasks.

The lab also supports dispatch-managed execution. That gives non-local or externally managed workers a package protocol without weakening the stable run ledger or cold-start contract.
