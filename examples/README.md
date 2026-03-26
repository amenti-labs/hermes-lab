# Examples

## Quick: Run a basic experiment

```bash
# 1. Initialize
export HERMES_LAB_DATA_ROOT=./my-lab-data
python3 scripts/labctl.py init

# 2. Create an experiment from the generic template
python3 scripts/labctl.py create templates/autoresearch-generic.yaml

# 3. Run it
python3 scripts/labctl.py run-once

# 4. Check results
python3 scripts/labctl.py status
```

## Example: Hyperparameter Search

Create a workspace directory with your training code and a `search_space.json`:

```bash
mkdir -p workspaces/my-experiment
```

`workspaces/my-experiment/search_space.json`:
```json
{
  "learning_rate": {"low": 1e-5, "high": 1e-1, "log": true, "type": "float"},
  "batch_size": {"low": 16, "high": 256, "type": "int"},
  "dropout": {"low": 0.0, "high": 0.5, "type": "float"}
}
```

`workspaces/my-experiment/train.py`:
```python
import json, sys

config = json.load(open("train_config.json"))
lr = config["learning_rate"]
bs = config["batch_size"]
dropout = config["dropout"]

# Your training code here...
accuracy = 0.85  # placeholder

# Write metrics for the lab
json.dump({"accuracy": accuracy}, open("metrics.json", "w"))
print(f"accuracy={accuracy}")
```

Create a SPEC template `my-experiment.yaml`:
```yaml
id: my-experiment
mode: autoresearch-local-agent
goal: Maximize accuracy by searching learning rate, batch size, and dropout.
metric: accuracy
metric_direction: maximize
priority: normal
autonomous: true
time_budget_minutes: 10
workspace_root: workspaces/my-experiment
executor_command: python3 train.py
validation_command: python3 train.py
```

Then:
```bash
python3 scripts/labctl.py create my-experiment.yaml
python3 scripts/labctl.py burst my-experiment --strategy random -n 20
```

## Example: Agent-Driven Code Mutation

Use any AI agent (recommended: [hermes-agent](https://github.com/amenti-labs/hermes-agent)) to iteratively improve code:

```bash
# Create from the local agent mutation template
python3 scripts/labctl.py create templates/local-agent-autoresearch.yaml
```

The agent will:
1. Read the current code and metrics
2. Propose a mutation
3. Run validation
4. Accept or reject based on metric improvement
5. Repeat
