# Examples

## Optimize a Function (no API keys needed)

The [optimize-function](optimize-function/) example runs end-to-end in under 2 minutes with zero external dependencies. It uses burst mode to minimize the Rosenbrock function via random search.

```bash
export HERMES_LAB_DATA_ROOT=./demo-data
python3 scripts/labctl.py init
python3 scripts/labctl.py create examples/optimize-function/spec.yaml
python3 scripts/labctl.py burst optimize-rosenbrock --strategy random -n 20
python3 scripts/labctl.py status
```

See [optimize-function/README.md](optimize-function/README.md) for details.

## Hyperparameter Search

Create a directory with your training code, a `search_space.json`, and a SPEC:

`my-experiment/search_space.json`:
```json
{
  "learning_rate": {"low": 1e-5, "high": 1e-1, "log": true, "type": "float"},
  "batch_size": {"low": 16, "high": 256, "type": "int"},
  "dropout": {"low": 0.0, "high": 0.5, "type": "float"}
}
```

`my-experiment/train.py`:
```python
import json
config = json.load(open("train_config.json"))
# Your training code here...
accuracy = 0.85
json.dump({"accuracy": accuracy}, open("metrics.json", "w"))
```

`my-experiment.yaml`:
```yaml
id: my-experiment
goal: Maximize accuracy by searching learning rate, batch size, and dropout.
metric: accuracy
metric_direction: maximize
workspace_root: my-experiment
executor_command: python3 train.py
validation_command: python3 train.py
```

```bash
python3 scripts/labctl.py create my-experiment.yaml
python3 scripts/labctl.py burst my-experiment --strategy random -n 20
```
