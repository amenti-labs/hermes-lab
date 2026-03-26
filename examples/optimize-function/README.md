# Optimize a Function — hermes-lab Quick-Start Example

Optimize the Rosenbrock function f(x,y) = (1-x)² + 100·(y-x²)²
using hermes-lab burst mode.  No API keys, no external agents —
runs entirely locally in under 2 minutes.

The global minimum is 0 at (x=1, y=1).  The lab maximizes the
negated value, so the best possible score is 0.

## Files

- `search_space.json` — parameter bounds for x ∈ [-5, 5] and y ∈ [-5, 5]
- `evaluate.py` — reads `train_config.json`, evaluates Rosenbrock, writes `metrics.json`
- `spec.yaml` — experiment specification for `labctl create`

## Run it (from repo root)

```bash
# 1. Point the lab at a scratch data directory
export HERMES_LAB_DATA_ROOT=./demo-data

# 2. Initialize the lab
python3 scripts/labctl.py init

# 3. Register the experiment
python3 scripts/labctl.py create examples/optimize-function/spec.yaml

# 4. Run 20 random-search iterations
python3 scripts/labctl.py burst optimize-rosenbrock --strategy random -n 20

# 5. Check results
python3 scripts/labctl.py status
```

After the burst you will see the best score found and its (x, y) values.
The closer the score is to 0 the better — that means x ≈ 1 and y ≈ 1.

## Try other strategies

```bash
# Bayesian optimization (if optuna is installed)
python3 scripts/labctl.py burst optimize-rosenbrock --strategy bayesian -n 30

# More iterations
python3 scripts/labctl.py burst optimize-rosenbrock --strategy random -n 100
```

## Clean up

```bash
rm -rf ./demo-data
```
