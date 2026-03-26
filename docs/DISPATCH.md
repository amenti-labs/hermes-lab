# Dispatch Protocol

_Queue, claim, execute, complete, and ingest for agent-managed work packages_

---

## Why this exists

`run-once` is correct for direct local execution, but a generic lab also needs a safe path for:

- remote workers
- heterogeneous machines
- external agent harnesses
- human-in-the-loop package execution

The dispatch protocol solves that without letting workers mutate canonical experiment folders directly.

## Queue stages

- `dispatch/ready/`
  Prepared packages waiting to be claimed.
- `dispatch/running/`
  Claimed packages owned by one worker.
- `dispatch/complete/`
  Finished packages waiting for ingest or already archived after ingest.

## Package layout

Each package contains:

- `dispatch.json`
- `README.md`
- `input/`
- `run/`

`input/` is a cold-start snapshot for the worker:

- repo contract files such as `AGENTS.md` and `LAB_MANIFEST.json`
- root lab summaries such as `LAB-STATUS.md`, `LAB-INDEX.json`, and `PROGRAM.md`
- experiment files such as `SPEC.yaml`, `RUNBOOK.md`, `SUMMARY.md`, `NEXT.md`, `context.md`, and `best.md`

`run/` is the only writable area during execution:

- `manifest.json`
- `plan.md`
- `RESULT.md`
- `metrics.json`
- `stdout.log`
- `stderr.log`
- `artifacts/`

## Commands

Prepare packages:

```bash
python3 scripts/labctl.py dispatch-ready --max-runs 1
```

Claim packages without executing them:

```bash
python3 scripts/labctl.py dispatch-claim --max-runs 1 --worker jetson-orin
```

Claim and execute packages through the configured `executor_command`:

```bash
python3 scripts/labctl.py dispatch-work --max-runs 1 --worker jetson-orin
```

Mark an externally executed running package complete:

```bash
python3 scripts/labctl.py dispatch-complete <dispatch_id>
```

Ingest sealed packages into canonical experiment history:

```bash
python3 scripts/labctl.py dispatch-ingest
python3 scripts/labctl.py dispatch-ingest <dispatch_id>
```

## Generic worker rule

If a worker is operating through dispatch, it should:

1. read `dispatch.json`, `input/RUNBOOK.md`, and `run/plan.md`
2. write only inside `run/`
3. produce `run/RESULT.md` and `run/metrics.json`
4. finish with `dispatch-complete` and `dispatch-ingest`

## Canonicality rule

Dispatch packages are not canonical experiment history by themselves.

The canonical run appears only after `dispatch-ingest` copies the sealed `run/` bundle into:

`experiments/<id>/runs/<run-id>/`

That keeps transport and execution staging separate from the stable experiment ledger.

## Agent-Driven Dispatch (No API Keys)

For agent harnesses (Hermes, Codex, etc.) that have their own model access, use the agent dispatch commands. These eliminate the need for API keys in the lab environment.

Set `agent_provider: dispatch` in your SPEC to use this mode.

### Get next work package

```bash
python3 scripts/labctl.py dispatch-agent-next --worker hermes
```

Returns a JSON blob with everything an agent needs:
- `dispatch_id`, `experiment`, `workspace_root`
- `mutable_paths`, `read_only_paths`, `validation_command`
- `mutation_brief_path` (path to the instruction file)
- `metric`, `metric_direction`, `best_metric_value`
- `input_files` (SUMMARY.md, NEXT.md, plan.md content)
- `current_files` (current content of mutable files)

Returns `{}` when no experiment is eligible.

### Submit agent changes

Write a JSON file with your proposed changes:

```json
{
  "dispatch_id": "DISPATCH-...",
  "changes": {
    "sampler_params.json": "{...new content...}"
  },
  "reasoning": "Enable light logit perturbation to break greedy patterns."
}
```

Then submit:

```bash
python3 scripts/labctl.py dispatch-agent-submit changes.json --worker hermes
```

This command handles the full pipeline:
1. Creates a sandboxed git clone of the workspace
2. Applies your file changes
3. Runs the validation_command
4. Compares the metric to best-so-far
5. Generates RESULT.md, metrics.json, diff.patch, decision.json
6. Marks complete and ingests into canonical experiment history

Returns a JSON result with `outcome`, `accepted`, `candidate_value`, etc.

### Full agent loop (no API keys needed)

```
while true:
  context = dispatch-agent-next
  if empty: sleep and retry
  agent reads context, proposes mutation
  dispatch-agent-submit with changes
  repeat
```

## Root visibility

`LAB-STATUS.md` and `LAB-INDEX.json` now include dispatch queue state so a root-pointed agent can see:

- counts by dispatch stage
- active package metadata
- per-experiment `current_dispatch`
