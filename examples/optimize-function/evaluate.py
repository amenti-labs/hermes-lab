#!/usr/bin/env python3
"""Evaluate the Rosenbrock function.

Reads x, y from train_config.json (written by the lab runner),
computes the negated Rosenbrock value (so higher = better),
and writes the result to metrics.json.

Rosenbrock: f(x,y) = (a - x)^2 + b * (y - x^2)^2
with a=1, b=100.  Global minimum is 0 at (1, 1).
We negate it so the lab's maximize direction finds the optimum.
"""
import json
import sys
from pathlib import Path


def rosenbrock(x: float, y: float, a: float = 1.0, b: float = 100.0) -> float:
    return (a - x) ** 2 + b * (y - x ** 2) ** 2


def main():
    config_path = Path("train_config.json")
    if not config_path.exists():
        print("ERROR: train_config.json not found", file=sys.stderr)
        sys.exit(1)

    config = json.loads(config_path.read_text())
    x = float(config["x"])
    y = float(config["y"])

    raw = rosenbrock(x, y)
    score = -raw  # negate so maximize -> find minimum

    metrics = {"score": score, "rosenbrock_raw": raw, "x": x, "y": y}
    Path("metrics.json").write_text(json.dumps(metrics, indent=2))

    print(f"x={x:.4f}  y={y:.4f}  rosenbrock={raw:.6f}  score={score:.6f}")


if __name__ == "__main__":
    main()
