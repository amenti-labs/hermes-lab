# Hermes Lab Operations

## Data Root

Default:

`$HERMES_LAB_DATA_ROOT` (default: `./lab-data`)

Override with an explicit local path:

```bash
export HERMES_LAB_DATA_ROOT=~/lab-data
```

Set `HERMES_LAB_DATA_ROOT` to your preferred path, or the default `./lab-data` is used.

## Main Commands

Initialize:

```bash
python3 scripts/labctl.py init
```

Create an experiment:

```bash
python3 scripts/labctl.py create templates/research-sprint.yaml
python3 scripts/labctl.py create templates/autoresearch-generic.yaml
python3 scripts/labctl.py create templates/code-autoresearch.yaml
python3 scripts/labctl.py create templates/local-agent-autoresearch.yaml
python3 scripts/labctl.py create templates/multifidelity-autoresearch.yaml
```

Compatibility alias:

```bash
python3 scripts/labctl.py add-task templates/research-sprint.yaml
```

Show status:

```bash
python3 scripts/labctl.py status
python3 scripts/labctl.py list
```

Run one scheduler cycle:

```bash
python3 scripts/labctl.py run-once --max-runs 3
python3 scripts/labctl.py run-once --max-runs 1 --executor-class jetson-orin
```

Dispatch flow:

```bash
python3 scripts/labctl.py dispatch-ready --max-runs 1
python3 scripts/labctl.py dispatch-claim --max-runs 1 --worker jetson-orin
python3 scripts/labctl.py dispatch-work --max-runs 1 --worker jetson-orin
python3 scripts/labctl.py dispatch-complete <dispatch_id>
python3 scripts/labctl.py dispatch-ingest
```

Pause / resume / complete:

```bash
python3 scripts/labctl.py pause <exp_id> --reason "manual hold"
python3 scripts/labctl.py resume <exp_id> --reason "resume after review"
python3 scripts/labctl.py complete <exp_id> --reason "done"
python3 scripts/labctl.py set-fidelity <exp_id> final --reason "promote to finalist runs"
```

Rebuild projections:

```bash
python3 scripts/labctl.py refresh
```

Digests:

```bash
python3 scripts/labctl.py digest
python3 scripts/labctl.py weekly-digest
```

Watchdog:

```bash
python3 scripts/labctl.py watchdog
python3 scripts/labctl.py watchdog --repair
```

## Shell Wrappers

These wrappers call the CLI:

- `bash scripts/init.sh`
- `bash scripts/run-cycle.sh`
- `bash scripts/write-digest.sh`
- `bash scripts/write-weekly-digest.sh`

## launchd

Render launchd plists with the current repo root and data root:

```bash
bash scripts/install-launchd.sh --no-load
```

Load them immediately instead:

```bash
bash scripts/install-launchd.sh
```

Installed agents:

- `com.example.hermes-lab.run-once`
- `com.example.hermes-lab.digest`
- `com.example.hermes-lab.weekly-digest`

## Suggested Rhythm

- `run-once` every 30 minutes
- `digest` every morning
- `weekly-digest` once a week
- `watchdog --repair` from the run cycle or on a short timer if desired

## Notes

- Command records are written into `control/inbox/`.
- Daily digest headlines are mirrored into `control/outbox/`.
- `LAB-INDEX.json` is the machine-readable root summary for agents and tools.
- `LAB-INDEX.json` also exposes dispatch queue counts and package metadata.
- If no `executor_command` is configured in the SPEC, the built-in stub still produces valid run bundles so the rest of the lab can be exercised safely.
- Root-level harness guidance lives in `AGENTS.md` and `LAB_MANIFEST.json`.
- Per-experiment harness guidance lives in `RUNBOOK.md`.
- `scripts/reference_executor.py` is the shipped adapter for sandboxed mutation, validation, and git-backed patch promotion.
- `scripts/local_agent_mutation.py` is the local provider router for `agent_provider`-style specs.
- `docs/MULTI-FIDELITY.md` describes the tier contract for proxy, validation, and final runs inside one experiment.
- `docs/LOCAL-AGENT-MUTATION.md` describes the provider-agnostic local mutation contract.
- `docs/DISPATCH.md` describes the queue/claim/complete/ingest protocol for agent-managed work packages.
