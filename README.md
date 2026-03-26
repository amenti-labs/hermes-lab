# Hermes Lab

File-first autonomous experiment scaffolding for long-running research controlled by AI agents or human operators.

Run hyperparameter sweeps, code mutation loops, or any iterative research process with built-in scheduling, dispatch, multi-fidelity tiers, and automatic result tracking.

## Why Hermes Lab?

Most ML experiment tools assume you're training models on GPUs. Hermes Lab is **domain-agnostic**: it manages any experiment that has a metric, a search space, and a way to run iterations. The lab is the scheduler and bookkeeper; you bring the executor.

- **File-first**: All state lives in plain files (YAML, JSON, Markdown). No database, no server.
- **Agent-native**: Built to be operated by AI coding agents (Claude Code, Codex, etc.) or humans via CLI.
- **Append-only history**: Every run is an immutable bundle. Derived views (summaries, rankings) are rebuilt from sealed runs.
- **Multi-fidelity**: Run cheap proxy experiments first, promote winners to expensive final validation.
- **Dispatch protocol**: Let remote workers or cloud GPUs claim work packages without direct filesystem access.

## Quick Start

```bash
# Clone the repo
git clone https://github.com/your-org/hermes-lab.git
cd hermes-lab

# Set your data root (or use the default ./lab-data)
export HERMES_LAB_DATA_ROOT=~/hermes-lab-data

# Initialize the lab
python3 scripts/labctl.py init

# Create an experiment from a template
python3 scripts/labctl.py create templates/autoresearch-generic.yaml

# Run one scheduler cycle
python3 scripts/labctl.py run-once

# Check status
python3 scripts/labctl.py status
```

## Execution Modes

| Mode | Description | Use when |
|------|-------------|----------|
| **Cron** | Autonomous background runs on a schedule | Unattended overnight search |
| **Burst** | Back-to-back iterations with auto-generated params | Fast exploration, you're watching |
| **Guided** | Pauses for approval before each iteration | Early experiment design, steering |
| **Swarm** | Multi-strategy rotation with shared blackboard | Broad search across approaches |

```bash
# Burst: 20 random iterations
python3 scripts/labctl.py burst <exp_id> --strategy random -n 20

# Guided: approve each step
python3 scripts/labctl.py guided <exp_id> --strategy perturb -n 10

# Swarm: rotate strategies
python3 scripts/labctl.py swarm <exp_id> --strategies random perturb bayesian -n 30
```

## Search Strategies

| Strategy | When to use |
|----------|-------------|
| `random` | Initial exploration, no prior knowledge |
| `perturb` | Refine near a good baseline |
| `bayesian` | 10+ trials done, want smarter suggestions (needs `optuna`) |
| `evolution` | Large search space, population-based (needs `nevergrad`) |
| `llm` | LLM reads summaries and proposes next params |

## Templates

Start from a template that matches your use case:

| Template | Description |
|----------|-------------|
| `autoresearch-generic.yaml` | Minimal starting point for any experiment |
| `code-autoresearch.yaml` | Git-backed code mutation loop |
| `local-agent-autoresearch.yaml` | Provider-agnostic local agent mutation |
| `openai-codex-autoresearch.yaml` | OpenAI-backed code mutation |
| `claude-autoresearch.yaml` | Claude-backed code mutation |
| `multifidelity-autoresearch.yaml` | Proxy/final two-tier experiments |
| `research-sprint.yaml` | Time-boxed research sprint |

## Data Root Layout

```text
$HERMES_LAB_DATA_ROOT/
├── README-FIRST.md
├── AGENT_ENTRY.md
├── LAB-STATUS.md
├── LAB-INDEX.json
├── PROGRAM.md
├── CHANGELOG.md
├── registry/
│   ├── events.jsonl
│   └── locks/
├── experiments/
│   └── <exp-id>/
│       ├── SPEC.yaml
│       ├── STATUS.json
│       ├── RUNBOOK.md
│       ├── SUMMARY.md
│       ├── NEXT.md
│       ├── context.md
│       ├── best.md
│       ├── metrics.jsonl
│       ├── checkpoints/
│       └── runs/
│           └── RUN-<timestamp>-<role>/
│               ├── manifest.json
│               ├── plan.md
│               ├── RESULT.md
│               ├── metrics.json
│               ├── stdout.log
│               ├── stderr.log
│               └── artifacts/
├── digests/{daily,weekly}
├── control/{inbox,approvals,outbox}
├── dispatch/{ready,running,complete}
├── backups/
└── scratch/
```

## Search Space

Every workspace needs a `search_space.json`:

```json
{
  "weight_decay": {"low": 0.1, "high": 10.0, "log": true, "type": "float"},
  "learning_rate": {"low": 1e-5, "high": 1e-2, "log": true, "type": "float"},
  "hidden_dim": {"low": 64, "high": 512, "type": "int"}
}
```

Flat params are auto-merged into nested configs.

## How It Works

`labctl run-once` does this for each eligible experiment:

1. Reclaim stale leases if needed.
2. Pick the next role from `worker_roles`.
3. Acquire a lease.
4. Create a run bundle and `plan.md`.
5. Run the configured `executor_command` (or built-in stub).
6. Seal the run bundle with `RESULT.md` and `metrics.json`.
7. Rebuild projections (`RUNBOOK.md`, `SUMMARY.md`, `LAB-STATUS.md`).
8. Release the lease.

## Dispatch Workflow

For workers that should not write directly into experiment folders:

```bash
python3 scripts/labctl.py dispatch-ready --max-runs 1
python3 scripts/labctl.py dispatch-claim --max-runs 1 --worker my-gpu
python3 scripts/labctl.py dispatch-work --max-runs 1 --worker my-gpu
python3 scripts/labctl.py dispatch-complete <dispatch_id>
python3 scripts/labctl.py dispatch-ingest
```

## Multi-Fidelity

Run cheap proxy experiments, promote winners to final validation:

```yaml
fidelity_tiers:
  - proxy
  - final
initial_fidelity_tier: proxy
proxy_executor_class: local-cpu
final_executor_class: cloud-h100
```

## Agent Integration

Hermes Lab is designed for AI agents. The `AGENTS.md` file at the repo root is the agent contract. `LAB_MANIFEST.json` is the machine-readable version.

Agents get structured context via environment variables:

- `LAB_DATA_ROOT`, `LAB_EXPERIMENT_ID`, `LAB_RUN_DIR`
- `LAB_PRIMARY_METRIC`, `LAB_METRIC_DIRECTION`
- `LAB_AGENT_PROVIDER`, `LAB_AGENT_MODEL`
- `LAB_WORKSPACE_ROOT`, `LAB_FIDELITY_TIER`
- ... and 30+ more (see `LAB_MANIFEST.json`)

## Repo Structure

```
lab/          Core runtime (scheduler, strategies, runner, recovery)
scripts/      CLI (labctl.py), executors, mutation adapters
templates/    SPEC templates and data-root document templates
config/       launchd plist templates
docs/         Architecture and operations docs
tests/        Test suite
AGENTS.md     Agent contract
```

## Full Command Reference

```bash
python3 scripts/labctl.py init                    # Initialize data root
python3 scripts/labctl.py create <template.yaml>  # Create experiment
python3 scripts/labctl.py run-once [--max-runs N]  # Run scheduler cycle
python3 scripts/labctl.py status                   # Lab status
python3 scripts/labctl.py list                     # List experiments
python3 scripts/labctl.py pause <exp_id>           # Pause experiment
python3 scripts/labctl.py resume <exp_id>          # Resume experiment
python3 scripts/labctl.py complete <exp_id>        # Mark complete
python3 scripts/labctl.py set-fidelity <id> <tier> # Change fidelity tier
python3 scripts/labctl.py digest                   # Generate daily digest
python3 scripts/labctl.py weekly-digest            # Generate weekly digest
python3 scripts/labctl.py refresh                  # Rebuild projections
python3 scripts/labctl.py watchdog --repair        # Fix stale state
python3 scripts/labctl.py recover                  # Full recovery
python3 scripts/labctl.py burst <id> -n N          # Burst mode
python3 scripts/labctl.py guided <id> -n N         # Guided mode
python3 scripts/labctl.py swarm <id> -n N          # Swarm mode
```

## Requirements

- Python 3.10+
- No required dependencies for core functionality
- Optional: `optuna` (Bayesian strategy), `nevergrad` (evolution strategy)

## License

MIT
