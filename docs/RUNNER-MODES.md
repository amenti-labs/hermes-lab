# Hermes Lab Runner Modes

Four ways to run experiments, all using the same dispatch loop underneath.

## Modes

### 1. Cron (existing)
Agent runs every N minutes autonomously via Hermes cron jobs.
```
# Can be configured as a cron job for autonomous background runs
```

### 2. Burst
Run N iterations back-to-back in the current session. Strategies auto-generate params.
```bash
# Random search, 20 iterations
labctl burst my-experiment --strategy random -n 20

# Bayesian optimization (Optuna TPE)
labctl burst my-experiment --strategy bayesian -n 50

# Perturb best (PBT-inspired)
labctl burst my-experiment --strategy perturb -n 30

# With custom search space
labctl burst my-experiment --strategy bayesian -n 20 --search-space /path/to/space.json
```

### 3. Guided
Like burst but pauses for your approval before each iteration.
```bash
labctl guided my-experiment --strategy perturb -n 10
```
At each step you see the proposed params and can:
- `Y` or Enter to approve
- `n` to skip
- `edit` to type custom JSON params

### 4. Swarm
Multiple strategies rotate, coordinating via a shared SQLite blackboard.
```bash
# Default strategies: random, perturb, bayesian
labctl swarm my-experiment -n 30

# Custom strategy mix
labctl swarm my-experiment --strategies random perturb bayesian evolution -n 40
```

The blackboard tracks all trials, claims (to prevent duplicate work), and a feed
of discoveries. Each strategy sees what the others have tried.

## Strategies

| Name      | How it works                              | Requires     |
|-----------|-------------------------------------------|--------------|
| random    | Uniform random within bounds              | nothing      |
| perturb   | Perturb current best params (PBT-style)   | nothing      |
| bayesian  | Optuna TPE-guided suggestions             | `pip install optuna` |
| evolution | Nevergrad CMA-ES optimization             | `pip install nevergrad` |
| tree      | AIDE-style tree search: branch or improve  | none                    |
| llm       | Pass-through for LLM-driven mutations     | agent callback |

## Search Space

Create `search_space.json` in the workspace root:
```json
{
  "weight_decay": {"low": 0.1, "high": 10.0, "log": true, "type": "float"},
  "learning_rate": {"low": 1e-5, "high": 1e-2, "log": true, "type": "float"},
  "dropout": {"low": 0.0, "high": 0.3, "type": "float"},
  "hidden_dim": {"low": 64, "high": 512, "type": "int"}
}
```

The runner auto-discovers `search_space.json` from the workspace. Override with `--search-space`.

Flat params are deep-merged into nested configs (e.g., `weight_decay` finds
`config["training"]["weight_decay"]` automatically).

## Common Options

```
-n, --iterations N     Max iterations (default: 10)
--seed N               Reproducible results
--direction maximize   Or minimize
--pause 5.0            Seconds between iterations
--worker name          Worker name in logs
```

## Architecture

```
labctl burst/guided/swarm
  -> lab/runner.py (scheduler)
    -> lab/strategies.py (ask/tell interface)
    -> lab/blackboard.py (SQLite coordination, swarm only)
    -> lab/core.py (dispatch_agent_next + dispatch_agent_submit)
```

All modes reuse the existing dispatch loop. Errors are caught, stale state is
cleaned up, and the next iteration proceeds.
